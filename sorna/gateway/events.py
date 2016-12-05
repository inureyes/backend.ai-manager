import asyncio
from collections import defaultdict
import functools
import logging

import aioredis
import aiozmq, aiozmq.rpc

from sorna import defs

log = logging.getLogger('sorna.gateway.events')


class EventServer(aiozmq.rpc.AttrHandler):

    def __init__(self, app, loop=None):
        self.app = app
        self.loop = loop if loop else asyncio.get_event_loop()
        self.handlers = defaultdict(list)

    def add_handler(self, event_name, callback):
        assert callable(callback)
        self.handlers[event_name].append(callback)

    def local_dispatch(self, event_name, *args, **kwargs):
        log.debug('DISPATCH({} {})'.format(event_name, str(args[0]) if args else ''))
        for handler in self.handlers[event_name]:
            if asyncio.iscoroutine(handler) or asyncio.iscoroutinefunction(handler):
                asyncio.ensure_future(handler(self.app, *args, **kwargs))
            else:
                cb = functools.partial(handler, self.app, *args, **kwargs)
                self.loop.call_soon(cb)

    @aiozmq.rpc.method
    def dispatch(self, event_name, *args, **kwargs):
        self.local_dispatch(event_name, *args, **kwargs)


async def monitor_redis_events(app):
    redis_sub = await aioredis.create_redis(app.config.redis_addr, encoding='utf8')
    # Enable "expired" event notification
    # See more details at: http://redis.io/topics/notifications
    await redis_sub.config_set('notify-keyspace-events', 'Ex')
    chprefix = '__keyevent@{}__*'.format(defs.SORNA_INSTANCE_DB)
    channels = await redis_sub.psubscribe(chprefix)
    log.debug('monitor_redis_events: subscribed notifications.')
    try:
        while True:
            msg = await channels[0].get(encoding='utf8')
            if msg is None:
                break
            evname = msg[0].decode('ascii').split(':')[1]
            evkey  = msg[1]
            if evname == 'expired' and evkey.startswith('shadow:'):
                inst_id = evkey.split(':', 1)[1]
                app['event_server'].local_dispatch('instance_terminated', 'agent-lost', inst_id)
    except asyncio.CancelledError:
        pass
    finally:
        await redis_sub.unsubscribe(chprefix)
        redis_sub.close()
        await redis_sub.wait_closed()


async def init(app):
    app['event_server'] = EventServer(app)
    app['event_sock'] = await aiozmq.rpc.serve_rpc(
        app['event_server'],
        bind='tcp://*:{}'.format(app.config.events_port))
    app['event_redis_monitor_task'] = asyncio.ensure_future(monitor_redis_events(app))


async def shutdown(app):
    app['event_redis_monitor_task'].cancel()
    await asyncio.sleep(0.01)
    app['event_sock'].close()
    await app['event_sock'].wait_closed()