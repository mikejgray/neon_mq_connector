# NEON AI (TM) SOFTWARE, Software Development Kit & Application Framework
# All trademark and other rights reserved by their respective owners
# Copyright 2008-2022 Neongecko.com Inc.
# Contributors: Daniel McKnight, Guy Daniels, Elon Gasper, Richard Leeds,
# Regina Bloomstine, Casimiro Ferreira, Andrii Pernatii, Kirill Hrymailo
# BSD-3 License
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS  BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS;  OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE,  EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import os
import copy
import time
import uuid
import pika
import pika.exceptions
import threading

from abc import ABC
from typing import Optional, Dict, Any, Union
from pika.exchange_type import ExchangeType
from ovos_utils.log import LOG

from neon_mq_connector.config import load_neon_mq_config
from neon_mq_connector.utils import RepeatingTimer, retry, wait_for_mq_startup
from neon_mq_connector.utils.network_utils import dict_to_b64


def _default_error_handler(*args):
    LOG.warning("Error handler not defined")
    raise Exception(*args)


class ConsumerThread(threading.Thread):

    # retry to handle connection failures in case MQ server is still starting
    def __init__(self, connection_params: pika.ConnectionParameters,
                 queue: str, callback_func: callable,
                 error_func: callable = _default_error_handler,
                 auto_ack: bool = True,
                 queue_reset: bool = False,
                 queue_exclusive: bool = False,
                 exchange: Optional[str] = None,
                 exchange_reset: bool = False,
                 exchange_type: str = ExchangeType.direct, *args, **kwargs):
        """
        Rabbit MQ Consumer class that aims at providing unified configurable
        interface for consumer threads
        :param connection_params: pika connection parameters
        :param queue: Desired consuming queue
        :param callback_func: logic on message receiving
        :param error_func: handler for consumer thread errors
        :param auto_ack: Boolean to enable ack of messages upon receipt
        :param queue_reset: If True, delete an existing queue `queue`
        :param queue_exclusive: Marks declared queue as exclusive
            to a given channel (deletes with it)
        :param exchange: exchange to bind queue to (optional)
        :param exchange_reset: If True, delete an existing exchange `exchange`
        :param exchange_type: type of exchange to bind to from ExchangeType
            (defaults to direct)
            follow: https://www.rabbitmq.com/tutorials/amqp-concepts.html
            to learn more about different exchanges
        """
        threading.Thread.__init__(self, *args, **kwargs)
        self._is_consuming = False  # annotates that ConsumerThread is running
        self._is_consumer_alive = True  # annotates that ConsumerThread is alive and shall be recreated
        self.connection = pika.BlockingConnection(connection_params)
        self.callback_func = callback_func
        self.error_func = error_func
        self.exchange = exchange or ''
        self.exchange_type = exchange_type or ExchangeType.direct
        self.queue = queue or ''
        self.channel = self.connection.channel()
        self.channel.basic_qos(prefetch_count=50)
        if queue_reset:
            self.channel.queue_delete(queue=self.queue)
        declared_queue = self.channel.queue_declare(queue=self.queue,
                                                    auto_delete=False,
                                                    exclusive=queue_exclusive)
        if self.exchange:
            if exchange_reset:
                self.channel.exchange_delete(exchange=self.exchange)
            self.channel.exchange_declare(exchange=self.exchange,
                                          exchange_type=self.exchange_type,
                                          auto_delete=False)
            self.channel.queue_bind(queue=declared_queue.method.queue,
                                    exchange=self.exchange)
        self.channel.basic_consume(on_message_callback=self.callback_func,
                                   queue=self.queue,
                                   auto_ack=auto_ack)

    @property
    def is_consumer_alive(self) -> bool:
        return self._is_consumer_alive

    @property
    def is_consuming(self) -> bool:
        return self._is_consuming

    def run(self):
        """Creating consumer channel"""
        if not self._is_consuming:
            try:
                super(ConsumerThread, self).run()
                self._is_consuming = True
                self.channel.start_consuming()
            except Exception as e:
                self._is_consuming = False
                if isinstance(e, pika.exceptions.ChannelClosed):
                    LOG.error(f"Channel closed by broker: {self.callback_func}")
                else:
                    LOG.error(e)
                    self.error_func(self, e)
                self.join(allow_restart=True)

    def join(self, timeout: Optional[float] = ..., allow_restart: bool = True) -> None:
        """Terminating consumer channel"""
        if self._is_consumer_alive:
            try:
                self.channel.stop_consuming()
                if self.channel.is_open:
                    self.channel.close()
                if self.connection.is_open:
                    self.connection.close()
            except Exception as x:
                LOG.error(x)
            finally:
                self._is_consuming = False
                if not allow_restart:
                    self._is_consumer_alive = False
                super(ConsumerThread, self).join(timeout=timeout)


class MQConnector(ABC):
    """
    Abstract class implementing interface for attaching services to MQ server
    """

    __run_retries__ = 5
    __max_consumer_restarts__ = -1
    __consumer_join_timeout__ = 10

    @staticmethod
    def init_config(config: Optional[dict] = None) -> dict:
        """ Initialize config from source data """
        config = config or load_neon_mq_config() or dict()
        config = config.get('MQ') or config
        return config

    def __init__(self, config: Optional[dict], service_name: str):
        """
            :param config: dictionary with current configurations.

            JSON Template of :param config :

            {
                "users": {
                    "<service_name>": {
                        "user": "<username of the service on mq server>",
                        "password": "<password of the service on mq server>"
                    }
                },
                "server": "<MQ Server hostname or IP>",
                "port": <MQ Server Port (default=5672)>,
                "<self.property_key (default='properties')>": {
                    <key of the configurable property>:<value of the configurable property>
                }
            }

            :param service_name: name of current service
       """
        self._config = config
        # Override self.property_key BEFORE base __init__ to initialise
        # properties under customized config location
        if not hasattr(self, 'property_key'):
            self.property_key = 'properties'
        self._service_id = None
        self.service_name = service_name
        self.consumers: Dict[str, ConsumerThread] = dict()
        self.consumer_properties = dict()
        self._vhost = None
        self._sync_thread = None
        self._observer_thread = None

        # Define properties and initialize them
        self.sync_period = 0
        self.observe_period = 0
        self.vhost_prefix = ""
        self.default_testing_prefix = 'test'
        self.testing_envs = set()
        self.testing_prefix_envs = None
        self.__init_configurable_properties()

    @property
    def config(self):
        if not self._config:
            self._config = self.init_config()
        return self._config

    @config.setter
    def config(self, new_config: dict):
        self._config = self.init_config(config=new_config)

    @property
    def service_config(self) -> dict:
        """ Returns current service config """
        return self.config.get('users', {}).get(self.service_name) or dict()

    @property
    def __basic_configurable_properties(self) -> Dict[str, Any]:
        """
        Mapping of basic configurable properties to their default values.
        WARNING: This method should be left untouched to prevent unexpected
        behaviour. To override values of the basic properties specify it in
        self.service_configurable_properties()
        """
        return {
            'sync_period': 10,  # in seconds
            'observe_period': 20,  # in seconds
            'vhost_prefix': '',  # Could be used for scalability purposes
            'default_testing_prefix': 'test',
            'testing_envs': (f'{self.service_name.upper()}_TESTING',
                             'MQ_TESTING',),  # order matters
            'testing_prefix_envs': (f'{self.service_name.upper()}'
                                    f'_TESTING_PREFIX',
                                    'MQ_TESTING_PREFIX',)  # order matters
        }

    @property
    def service_configurable_properties(self) -> Dict[str, Any]:
        """
        Mapping of service-related configurable properties to default values.
        Override to provide service-specific configurable properties AND to
        update the default values of basic properties
        """
        return {}

    @property
    def __configurable_properties(self):
        """
        Joins basic configurable properties with appended once
        WARNING: This method should NOT be modified by children to prevent
        unexpected behaviour
        """
        return {**self.__basic_configurable_properties,
                **self.service_configurable_properties}

    def __init_configurable_properties(self):
        """
        Initialize properties based on the config and configurable properties
        WARNING: This method should NOT be modified by children to prevent
        unexpected behaviour
        """
        for _property, default_value in self.__configurable_properties.items():
            setattr(self, _property,
                    self.service_config.get(self.property_key,
                                            {}).get(_property, default_value))

    @property
    def service_id(self):
        """
        ID of the service should be considered to be unique
        """
        if not self._service_id:
            self._service_id = self.create_unique_id()
        return self._service_id

    @property
    def mq_credentials(self):
        """
        Returns MQ Credentials object based on self.config values
        """
        if not self.service_config or self.service_config == dict():
            raise Exception(f'Configuration is not set for {self.service_name}')
        return pika.PlainCredentials(
            self.service_config.get('user', 'guest'),
            self.service_config.get('password', 'guest'))

    @property
    def testing_mode(self) -> bool:
        """
        Indicates if given instance is instantiated in testing mode
        """
        return any(os.environ.get(env_var, '0') == '1'
                   for env_var in self.testing_envs)

    @property
    def testing_prefix(self) -> str:
        """
        Returns testing mode prefix for the item
        """
        for env_var in self.testing_prefix_envs:
            prefix = os.environ.get(env_var)
            if prefix:
                return prefix
        return self.default_testing_prefix

    @property
    def vhost(self):
        if not self._vhost:
            self._vhost = '/'
        if self.vhost_prefix and self.vhost_prefix not in \
                self._vhost.split('_')[0]:
            self._vhost = f'/{self.vhost_prefix}_{self._vhost[1:]}'
        if self.testing_mode and self.testing_prefix not in \
                self._vhost.split('_')[0]:
            self._vhost = f'/{self.testing_prefix}_{self._vhost[1:]}'
        if self._vhost.endswith('_'):
            self._vhost = self._vhost[:-1]
        return self._vhost

    @vhost.setter
    def vhost(self, val: str):
        if not val:
            val = ''
        elif not isinstance(val, str):
            val = str(val)
        if not val.startswith('/'):
            val = f'/{val}'
        self._vhost = val

    def get_connection_params(self, vhost: str, **kwargs) -> \
            pika.ConnectionParameters:
        """
        Gets connection parameters to be used to create an mq connection
        :param vhost: virtual_host to connect to
        """
        connection_params = pika.ConnectionParameters(
            host=self.config.get('server', 'localhost'),
            port=int(self.config.get('port', '5672')),
            virtual_host=vhost,
            credentials=self.mq_credentials, **kwargs)
        return connection_params

    @staticmethod
    def create_unique_id():
        """Method for generating unique id"""
        return uuid.uuid4().hex

    @classmethod
    def emit_mq_message(cls,
                        connection: pika.BlockingConnection,
                        request_data: dict,
                        exchange: Optional[str] = '',
                        queue: Optional[str] = '',
                        exchange_type: Union[str, ExchangeType] =
                        ExchangeType.direct,
                        expiration: int = 1000) -> str:
        """
        Emits request to the neon api service on the MQ bus
        :param connection: pika connection object
        :param queue: name of the queue to publish in
        :param request_data: dictionary with the request data
        :param exchange: name of the exchange (optional)
        :param exchange_type: type of exchange to declare
            (defaults to direct)
        :param expiration: mq message expiration time in millis
            (defaults to 1 second)

        :raises ValueError: invalid request data provided
        :returns message_id: id of the sent message
        """
        if not isinstance(request_data, dict):
            raise TypeError(f"Expected dict and got {type(request_data)}")
        if not request_data:
            raise ValueError(f'No request data provided')

        # Ensure `message_id` in data will match context in messagebus connector
        request_data.setdefault('message_id', request_data.get("context", {})
                                .get("mq", {}).get("message_id") or
                                cls.create_unique_id())

        with connection.channel() as channel:
            if exchange:
                channel.exchange_declare(exchange=exchange,
                                         exchange_type=exchange_type,
                                         auto_delete=False)
            if queue:
                declared_queue = channel.queue_declare(queue=queue,
                                                       auto_delete=False)
                if exchange_type == ExchangeType.fanout.value:
                    channel.queue_bind(queue=declared_queue.method.queue,
                                       exchange=exchange)
            channel.basic_publish(exchange=exchange or '',
                                  routing_key=queue,
                                  body=dict_to_b64(request_data),
                                  properties=pika.BasicProperties(
                                      expiration=str(expiration)))
        LOG.debug(f"sent message: {request_data['message_id']}")
        return request_data['message_id']

    @classmethod
    def publish_message(cls,
                        connection: pika.BlockingConnection,
                        request_data: dict,
                        exchange: Optional[str] = '',
                        expiration: int = 1000) -> str:
        """
        Publishes message via fanout exchange, wrapper for emit_mq_message
        :param connection: pika connection object
        :param request_data: dictionary with the request data
        :param exchange: name of the exchange (optional)
        :param expiration: mq message expiration time in millis
            (defaults to 1 second)

        :raises ValueError: invalid request data provided
        :returns message_id: id of the sent message
        """
        return cls.emit_mq_message(connection=connection,
                                   request_data=request_data, exchange=exchange,
                                   queue='', exchange_type='fanout',
                                   expiration=expiration)

    def send_message(self,
                     request_data: dict,
                     vhost: str = '',
                     connection_props: dict = None,
                     exchange: Optional[str] = '',
                     queue: Optional[str] = '',
                     exchange_type: ExchangeType = ExchangeType.direct,
                     expiration: int = 1000) -> str:
        """
        Wrapper method for creation the MQ connection and immediate propagation
        of requested message with that

        :param request_data: dictionary containing requesting data
        :param vhost: MQ Virtual Host (if not specified, uses its object native)
        :param exchange: MQ Exchange name (optional)
        :param queue: MQ Queue name (optional for ExchangeType.fanout)
        :param connection_props: supportive connection properties while
            connection creation (optional)
        :param exchange_type: type of exchange to use
            (defaults to ExchangeType.direct)
        :param expiration: posted data expiration (in millis)

        :returns message_id: id of the propagated message
        """
        if not vhost:
            vhost = self.vhost
        if not connection_props:
            connection_props = {}
        LOG.debug(f'Opening connection on vhost={vhost} queue={queue}')
        with self.create_mq_connection(vhost=vhost,
                                       **connection_props) as mq_conn:
            if exchange_type in (ExchangeType.fanout,
                                 ExchangeType.fanout.value,):
                LOG.debug(f'Sending fanout request to exchange: {exchange}')
                msg_id = self.publish_message(connection=mq_conn,
                                              request_data=request_data,
                                              exchange=exchange,
                                              expiration=expiration)
            else:
                LOG.debug(f'Sending {exchange_type} request to exchange '
                          f'{exchange}')
                msg_id = self.emit_mq_message(mq_conn,
                                              queue=queue,
                                              request_data=request_data,
                                              exchange=exchange,
                                              exchange_type=exchange_type,
                                              expiration=expiration)
        LOG.debug(f'Message propagated, id={msg_id}')
        return msg_id

    @retry(use_self=True, num_retries=__run_retries__)
    def create_mq_connection(self, vhost: str = '/', **kwargs):
        """
            Creates MQ Connection on the specified virtual host
            Note: Additional parameters can be defined via kwargs.

            :param vhost: address for desired virtual host
            :raises Exception if self.config is not set
        """
        if not self.config:
            raise Exception('Configuration is not set')
        return pika.BlockingConnection(
            parameters=self.get_connection_params(vhost, **kwargs))

    def register_consumer(self, name: str, vhost: str, queue: str,
                          callback: callable,
                          on_error: Optional[callable] = None,
                          auto_ack: bool = True, queue_reset: bool = False,
                          exchange: str = None, exchange_type: str = None,
                          exchange_reset: bool = False,
                          queue_exclusive: bool = False,
                          skip_on_existing: bool = False,
                          restart_attempts: int = __max_consumer_restarts__):
        """
        Registers a consumer for the specified queue.
        The callback function will handle items in the queue.
        Any raised exceptions will be passed as arguments to on_error.
        :param name: Human readable name of the consumer
        :param vhost: vhost to register on
        :param queue: MQ Queue to read messages from
        :param queue_reset: to delete queue if exists (defaults to False)
        :param exchange: MQ Exchange to bind to
        :param exchange_reset: to delete exchange if exists (defaults to False)
        :param exchange_type: Type of MQ Exchange to use, documentation:
            https://www.rabbitmq.com/tutorials/amqp-concepts.html
        :param callback: Method to passed queued messages to
        :param on_error: Optional method to handle any exceptions
            raised in message handling
        :param auto_ack: Boolean to enable ack of messages upon receipt
        :param queue_exclusive: if Queue needs to be exclusive
        :param skip_on_existing: to skip if consumer already exists
        :param restart_attempts: max instance restart attempts
            (if < 0 - will restart infinitely times)
        """
        error_handler = on_error or self.default_error_handler
        consumer = self.consumers.get(name, None)
        if consumer:
            # Gracefully terminating
            if skip_on_existing:
                LOG.info(f'Consumer under index "{name}" already declared')
                return
            self.stop_consumers(names=(name,), allow_restart=False)
        self.consumer_properties.setdefault(name, {})
        self.consumer_properties[name]['properties'] = \
            dict(connection_params=self.get_connection_params(vhost),
                 queue=queue, queue_reset=queue_reset, callback_func=callback,
                 exchange=exchange, exchange_reset=exchange_reset,
                 exchange_type=exchange_type, error_func=error_handler,
                 auto_ack=auto_ack, name=name, queue_exclusive=queue_exclusive,)
        self.consumer_properties[name]['restart_attempts'] = \
            int(restart_attempts)
        self.consumer_properties[name]['started'] = False
        self.consumers[name] = \
            ConsumerThread(**self.consumer_properties[name]['properties'])

    def restart_consumer(self, name: str):
        self.stop_consumers(names=(name,), allow_restart=True)
        consumer_data = self.consumer_properties.get(name, {})
        restart_attempts = consumer_data.get('restart_attempts',
                                             self.__max_consumer_restarts__)
        err_msg = ''
        if not consumer_data.get('is_alive', True):
            LOG.debug(f'Skipping joined consumer = "{name}"')
        elif not consumer_data.get('properties'):
            err_msg = 'creation properties not found'
        elif 0 < restart_attempts < consumer_data.get('num_restarted', 0):
            err_msg = 'num restarts exceeded'
        else:
            self.consumers[name] = ConsumerThread(**consumer_data['properties'])
            self.run_consumers(names=(name,))
            self.consumer_properties[name].setdefault('num_restarted', 0)
            self.consumer_properties[name]['num_restarted'] += 1
        if err_msg:
            LOG.error(f'Cannot restart consumer "{name}" - {err_msg}')

    def register_subscriber(self, name: str, vhost: str,
                            callback: callable,
                            on_error: Optional[callable] = None,
                            exchange: str = None, exchange_reset: bool = False,
                            auto_ack: bool = True,
                            skip_on_existing: bool = False,
                            restart_attempts: int = __max_consumer_restarts__):
        """
        Registers fanout exchange subscriber, wraps register_consumer()
        Any raised exceptions will be passed as arguments to on_error.
        :param name: Human readable name of the consumer
        :param vhost: vhost to register on
        :param exchange: MQ Exchange to bind to
        :param exchange_reset: to delete exchange if exists
            (defaults to False)
        :param callback: Method to passed queued messages to
        :param on_error: Optional method to handle any exceptions raised
            in message handling
        :param auto_ack: Boolean to enable ack of messages upon receipt
        :param skip_on_existing: to skip if consumer already exists
            (defaults to False)
        :param restart_attempts: max instance restart attempts
            (if < 0 - will restart infinitely times)
        """
        # for fanout exchange queue does not matter unless its non-conflicting
        # and is binded
        subscriber_queue = f'subscriber_{exchange}_{uuid.uuid4().hex[:6]}'
        LOG.info(f'Subscriber queue registered: {subscriber_queue} '
                 f'[subscriber_name={name},exchange={exchange},vhost={vhost}]')
        return self.register_consumer(name=name, vhost=vhost,
                                      queue=subscriber_queue,
                                      callback=callback, queue_reset=False,
                                      on_error=on_error, exchange=exchange,
                                      exchange_type=ExchangeType.fanout.value,
                                      exchange_reset=exchange_reset,
                                      auto_ack=auto_ack, queue_exclusive=True,
                                      skip_on_existing=skip_on_existing,
                                      restart_attempts=restart_attempts)

    @staticmethod
    def default_error_handler(thread: ConsumerThread, exception: Exception):
        LOG.error(f"{exception} occurred in {thread}")

    def run_consumers(self, names: tuple = (), daemon=True):
        """
        Runs consumer threads based on the name if present
        (starts all of the declared consumers by default)

        :param names: names of consumers to consider
        :param daemon: to kill consumer threads once main thread is over
        """
        if not names or len(names) == 0:
            names = list(self.consumers)
        for name in names:
            if isinstance(self.consumers.get(name), ConsumerThread) and self.consumers[name].is_consumer_alive:
                self.consumers[name].daemon = daemon
                self.consumers[name].start()
                self.consumer_properties[name]['started'] = True

    def stop_consumers(self, names: tuple = (), allow_restart: bool = True):
        """
            Stops consumer threads based on the name if present
            (stops all of the declared consumers by default)
        """
        if not names or len(names) == 0:
            names = list(self.consumers)
        for name in names:
            try:
                if name in list(self.consumers):
                    self.consumers[name].join(timeout=self.__consumer_join_timeout__, allow_restart=allow_restart)
                    self.consumer_properties[name]['is_alive'] = self.consumers[name].is_consumer_alive
                    self.consumer_properties[name]['started'] = False
                    self.consumers[name] = None
            except Exception as e:
                raise ChildProcessError(e)

    @retry(callback_on_exceeded='stop_sync_thread', use_self=True,
           num_retries=__run_retries__)
    def sync(self, vhost: str = None, exchange: str = None, queue: str = None,
             request_data: dict = None):
        """
        Periodic notification message to be sent into MQ,
        used to notify other network listeners about this service health status

        :param vhost: mq virtual host (defaults to self.vhost)
        :param exchange: mq exchange (defaults to base one)
        :param queue: message queue prefix (defaults to self.service_name)
        :param request_data: data to publish in sync
        """
        vhost = vhost or self.vhost
        queue = f'{queue or self.service_name}_sync'
        exchange = exchange or ''
        request_data = request_data or {'service_id': self.service_id,
                                        'time': int(time.time())}

        with self.create_mq_connection(vhost=vhost) as mq_connection:
            LOG.debug(f'Emitting sync message to (vhost="{vhost}",'
                      f' exchange="{exchange}", queue="{queue}")')
            self.publish_message(mq_connection, exchange=exchange,
                                 request_data=request_data)

    @retry(callback_on_exceeded='stop', use_self=True,
           num_retries=__run_retries__)
    def run(self, run_consumers: bool = True, run_sync: bool = True,
            run_observer: bool = True, **kwargs):
        """
        Generic method called on running the instance

        :param run_consumers: to run this instance consumers (defaults to True)
        :param run_sync: to run synchronization thread (defaults to True)
        :param run_observer: to run consumers state observation
            (defaults to True)
        """
        host = self.config.get('server', 'localhost')
        port = int(self.config.get('port', '5672'))
        wait_for_mq_startup(host, port)
        kwargs.setdefault('consumer_names', ())
        kwargs.setdefault('daemonize_consumers', False)
        self.pre_run(**kwargs)
        if run_consumers:
            self.run_consumers(names=kwargs['consumer_names'],
                               daemon=kwargs['daemonize_consumers'])
        if run_sync:
            self.sync_thread.start()
        if run_observer:
            self.observer_thread.start()
        self.post_run(**kwargs)

    @property
    def sync_thread(self):
        """Creates new synchronization thread if none is present"""
        if not (isinstance(self._sync_thread, RepeatingTimer) and
                self._sync_thread.is_alive()):
            self._sync_thread = RepeatingTimer(self.sync_period, self.sync)
            self._sync_thread.daemon = True
        return self._sync_thread

    def stop_sync_thread(self):
        """Stops synchronization thread and dereferences it"""
        if self._sync_thread:
            self._sync_thread.cancel()
            self._sync_thread = None

    def observe_consumers(self):
        """
        Iteratively observes each consumer, and if it was launched but is not
        alive - restarts it
        """
        # LOG.debug('Observers state observation')
        consumers_dict = copy.copy(self.consumers)
        for consumer_name, consumer_instance in consumers_dict.items():
            if self.consumer_properties[consumer_name]['started'] and \
                    not (isinstance(consumer_instance, ConsumerThread)
                         and consumer_instance.is_alive()
                         and consumer_instance.is_consuming):
                LOG.info(f'Consumer "{consumer_name}" is dead, restarting')
                self.restart_consumer(name=consumer_name)

    @property
    def observer_thread(self):
        """Creates new observer thread if none is present"""
        if not (isinstance(self._observer_thread, RepeatingTimer) and
                self._observer_thread.is_alive()):
            self._observer_thread = RepeatingTimer(self.observe_period,
                                                   self.observe_consumers)
            self._observer_thread.daemon = True
        return self._observer_thread

    def stop_observer_thread(self):
        """Stops observer thread and dereferences it"""
        if self._observer_thread:
            self._observer_thread.cancel()
            self._observer_thread = None

    def stop(self):
        """Generic method for graceful instance stopping"""
        self.stop_consumers(allow_restart=False)
        self.stop_sync_thread()
        self.stop_observer_thread()

    def pre_run(self, **kwargs):
        """Additional logic invoked before method run()"""
        pass

    def post_run(self, **kwargs):
        """Additional logic invoked after method run()"""
        pass
