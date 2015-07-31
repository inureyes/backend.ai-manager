#! /usr/bin/env python3

'''
The Sorna Drivers

A driver defines a set of methods to deploy and destroy computing resources,
such as Docker containers on top of AWS or the local machine.
'''

from abc import ABCMeta, abstractmethod
import asyncio, aiozmq
from enum import Enum
import uuid
from .structs import Kernel

__all__ = ['DriverTypes', 'BaseDriver', 'AWSDockerDriver', 'LocalDriver', 'create_driver']

DriverTypes = Enum('DriverTypes', 'local aws_docker')
AgentPortRange = tuple(range(5002, 5010))


class BaseDriver(metaclass=ABCMeta):
    '''
    The driver is a low-level interface to control computing resource infrastucture,
    such as Amazon AWS and local development environment.

    Drivers are agnostic to resource management polices; they just do what the
    registry and other components request.
    '''

    def __init__(self, loop=None):
        self.loop = loop if loop else asyncio.get_event_loop()

    @asyncio.coroutine
    @abstractmethod
    def launch_instance(self, spec=None):
        '''
        Prepare a VM instance and runs the sorna agent on it.

        :param spec: An object describing the type of instance and extra options sepcific to each driver. If None, the driver uses its default setting.

        :returns: A tuple of the instance ID and its IP address.
        '''
        raise NotImplementedError()

    @asyncio.coroutine
    @abstractmethod
    def destroy_instance(self, inst_id):
        '''
        Destroy the launched instance.
        '''
        raise NotImplementedError()

    @asyncio.coroutine
    @abstractmethod
    def create_kernel(self, instance, agent_port):
        '''
        Launch the kernel and return its ID.

        :param instance: An object containing information of the target instance.
        :type instance: :class:`Instance <sorna.structs.Instance>`

        :returns: A :class:`Kernel <sorna.structs.Kernel>` object with kernel details.
        '''
        raise NotImplementedError()

    @asyncio.coroutine
    @abstractmethod
    def destroy_kernel(self, kernel):
        '''
        Destroy the kernel.

        :param kernel: An object containing information of the target kernel.
        :type kernel: :class:`Kernel <sorna.structs.Kernel>`
        '''
        raise NotImplementedError()


class AWSDockerDriver(BaseDriver):
    '''
    This driver uses Amazon EC2 for instances and docker as the kernel container.
    '''

    @asyncio.coroutine
    def launch_instance(self, spec=None):
        if sepc is None:
            spec = 't2.micro'
        # TODO: use boto to launch EC2 instance
        raise NotImplementedError()

    @asyncio.coroutine
    def destroy_instance(self, inst_id):
        # TODO: use boto to destroy EC2 instance
        raise NotImplementedError()

    @asyncio.coroutine
    def create_kernel(self, instance, agent_port):
        cli = docker.Client(
            base_url='tcp://{0}:{1}'.format(instance.ip, instance.docker_port),
            timeout=5, version='auto'
        )
        # TODO: create the container image
        # TODO: change the command to "python3 -m sorna.kernel_agent"
        # TODO: pass agent_port
        container = cli.create_container(image='lablup-python-kernel:latest',
                                         command='/usr/bin/python3')
        kernel = Kernel(instance=instance, id=container.id)
        kernel.priv = container.id
        kernel.id = 'docker-{0}/{1}'.format(instance.ip, kernel.priv)
        kernel.agent_sock = 'tcp://{0}:{1}'.format(instance.ip, agent_port)
        # TODO: run the container and set the port mappings
        return kernel

    @asyncio.coroutine
    def destroy_kernel(self, kernel):
        # TODO: destroy the container
        raise NotImplementedError()


class LocalDriver(BaseDriver):
    '''
    This driver does not use remote hosts at all.
    The kernel container is simply a local process.
    '''

    def __init__(self, loop=None):
        super().__init__(loop=loop)
        self.agents = dict()

    @asyncio.coroutine
    def launch_instance(self, spec=None):
        # As this is the local machine, we do nothing!
        inst_id = str(uuid.uuid4())
        return inst_id, '127.0.0.1'

    @asyncio.coroutine
    def destroy_instance(self, inst_id):
        pass

    @asyncio.coroutine
    def create_kernel(self, instance, agent_port):
        kernel_id = 'local/{0}'.format(uuid.uuid4())
        kernel = Kernel(id=kernel_id, instance=instance)
        cmdargs = ('/usr/bin/env', 'python3', '-m', 'sorna.agent',
                   '--kernel-id', kernel_id, '--agent-port', str(agent_port))
        proc = yield from asyncio.create_subprocess_exec(*cmdargs, loop=self.loop)
        kernel.agent_sock = 'tcp://{0}:{1}'.format(instance.ip, agent_port)
        self.agents[kernel_id] = proc
        return kernel

    @asyncio.coroutine
    def destroy_kernel(self, kernel):
        proc = self.agents[kernel.id]
        proc.terminate()
        yield from proc.wait()
        del self.agents[kernel.id]


_driver_type_to_class = {
    'local': LocalDriver,
    'aws_docker': AWSDockerDriver,
}

def create_driver(name):
    '''
    Create an instance of driver with the given driver name.
    '''
    cls = _driver_type_to_class[name]
    return cls()