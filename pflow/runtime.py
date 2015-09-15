#!/usr/bin/env python
from .executors.single_process import SingleProcessGraphExecutor

import os
import sys
import uuid
import logging
import socket
import json
from abc import ABCMeta, abstractmethod
from collections import OrderedDict
import inspect
import functools
import textwrap

import argparse
import requests
import gevent
import geventwebsocket

import pflow.components
from . import exc, core, utils

log = logging.getLogger(__name__)


class Runtime(object):
    """
    Description of the runtime goes here.
    This will appear in the FlowHub registry description.
    """
    PROTOCOL_VERSION = '0.5'

    # Mapping of native Python types to FBP protocol types
    _type_map = {
        str: 'string',
        unicode: 'string',
        bool: 'boolean',
        int: 'int',
        float: 'number',
        complex: 'number',
        dict: 'object',
        list: 'array',
        tuple: 'array',
        set: 'array',
        frozenset: 'array',
        #color
        #date
        #function
        #buffer
    }

    def __init__(self, executor_class=SingleProcessGraphExecutor):
        self.log = logging.getLogger('%s.%s' % (self.__class__.__module__,
                                                self.__class__.__name__))

        self._components = {}  # Component metadata, keyed by component name
        self._graphs = {}  # Graph instances, keyed by graph ID
        self._executors = {}  # GraphExecutor instances, keyed by graph ID

        self.executor_class = executor_class

        self.log.debug('Initialized runtime!')

    def get_runtime_meta(self):
        # Supported protocol capabilities
        capabilities = [
            'protocol:runtime',      # expose the ports of its main graph using the Runtime protocol and transmit
                                     # packet information to/from them

            'protocol:graph',        # modify its graphs using the Graph protocol

            'protocol:component',    # list and modify its components using the Component protocol

            'protocol:network',      # control and introspect its running networks using the Network protocol

            #'component:setsource',  # compile and run custom components sent as source code strings

            'component:getsource',   # read and send component source code back to client

            'network:persist',       # "flash" a running graph setup into itself, making it persistent across reboots
        ]

        all_capabilities = capabilities

        return {
            'label': 'pflow python runtime',
            'type': 'pflow',
            'version': self.PROTOCOL_VERSION,
            'capabilities': capabilities,
            'allCapabilities': all_capabilities
            #'graph': ''
        }

    def get_all_component_specs(self):
        specs = {}
        for component_name, component_options in self._components.iteritems():
            specs[component_name] = component_options['spec']

        return specs

    def register_component(self, component_class, overwrite=False):
        """
        Registers a component class.

        :param component_class: the Component class to register.
        :param overwrite: should the component be overwritten if it already exists?
                if not, a ValueError will be raised if the component already exists.
        """
        if not issubclass(component_class, core.Component):
            raise ValueError('component_class must be a class that inherits '
                             'from Component')

        long_name = self._long_class_name(component_class)
        short_name = self._short_class_name(component_class)

        if long_name in self._components and not overwrite:
            raise ValueError("Component {0} already registered".format(
                long_name))

        self.log.debug('Registering component: {0}'.format(long_name))

        self._components[long_name] = {
            'class': component_class,
            'spec': self._create_component_spec(long_name, component_class)
        }

    def _long_class_name(self, component_class):
        return '{0}/{1}'.format(component_class.__module__,
                                component_class.__name__)

    def _short_class_name(self, component_class):
        return component_class.__name__

    def register_module(self, module, overwrite=False):
        """

        :param module:
        :param collection:
        :param overwrite:
        """
        if isinstance(module, basestring):
            module = __import__(module)

        if not inspect.ismodule(module):
            raise ValueError('module must be either a module or the name of a '
                             'module')

        self.log.debug('Registering components in module: {}'.format(
            module.__name__))

        registered = 0
        for obj_name in dir(module):
            class_obj = getattr(module, obj_name)
            if (inspect.isclass(class_obj) and
                    (class_obj != core.Component) and
                    (not inspect.isabstract(class_obj)) and
                    (not issubclass(class_obj, core.Graph)) and
                    issubclass(class_obj, core.Component)):
                self.register_component(class_obj, overwrite)
                registered += 1

        if registered == 0:
            self.log.warn('No components were found in module: {}'.format(
                module.__name__))

    def _create_component_spec(self, component_class_name, component_class):
        if not issubclass(component_class, core.Component):
            raise ValueError('component_class must be a Component')

        component = component_class('FAKE_NAME')

        def get_port_type(port, default_type='any'):
            if len(port.allowed_types) == 0:
                return default_type
            elif len(port.allowed_types) == 1:
                first_type = next(iter(port.allowed_types))
                mapped_type = self._type_map.get(first_type, default_type)
                # self.log.warn('Type of %s is %s' % (port, mapped_type))
                return mapped_type
            else:
                self.log.warn('{} has more than 1 allowed type, which is incompatible with FBP protocol. '
                              'Defaulting to "{}" instead.'.format(port, default_type))
                return default_type

        return {
            'name': component_class_name,
            'description': textwrap.dedent(component.__doc__ or '').strip(),
            #'icon': '',
            'subgraph': issubclass(component_class, core.Graph),
            'inPorts': [
                {
                    'id': inport.name,
                    'type': get_port_type(inport),
                    'description': (inport.description or ''),
                    'addressable': isinstance(inport, core.ArrayInputPort),
                    'required': (not inport.optional),
                    #'values': []
                    'default': inport.default
                }
                for inport in component.inputs
            ],
            'outPorts': [
                {
                    'id': outport.name,
                    'type': get_port_type(outport),
                    'description': (outport.description or ''),
                    'addressable': isinstance(outport, core.ArrayOutputPort),
                    'required': (not outport.optional)
                }
                for outport in component.outputs
            ]
        }

    def is_started(self, graph_id):
        if graph_id not in self._executors:
            return False

        return self._executors[graph_id].is_running()

    def start(self, graph_id):
        """
        Execute a graph.
        """
        self.log.debug('Graph {}: Starting execution'.format(graph_id))

        graph = self._graphs[graph_id]

        if graph_id not in self._executors:
            # Create executor
            self.log.info('Creating executor for graph {}...'.format(graph_id))
            executor = self._executors[graph_id] = self.executor_class(graph)
        else:
            executor = self._executors[graph_id]

        if executor.is_running():
            raise ValueError('Graph {} is already started'.format(graph_id))

        # gevent.spawn(executor.execute)
        # FIXME: single threaded runtime blocks here (use gevent.Group.spawn above):
        executor.execute()

        # TODO: send 'started' message back

    def stop(self, graph_id):
        """
        Stop executing a graph.
        """
        self.log.debug('Graph {}: Stopping execution'.format(graph_id))
        if graph_id not in self._executors:
            raise ValueError('Invalid graph: {}'.format(graph_id))

        executor = self._executors[graph_id]
        executor.stop()
        del self._executors[graph_id]

    def _create_or_get_graph(self, graph_id):
        """
        Parameters
        ----------
        graph_id : str
            unique identifier for the graph to create or get

        Returns
        -------
        graph : ``core.Graph``
            the graph object.
        """
        if graph_id not in self._graphs:
            self._graphs[graph_id] = core.Graph(graph_id, initialize=False)

        return self._graphs[graph_id]

    def _find_component_by_name(self, graph, component_name):
        for component in graph.components:
            if component.name == component_name:
                return component

    def get_source_code(self, component_name):
        component = None
        for graph in self._graphs.values():
            component = self._find_component_by_name(graph, component_name)
            if component is not None:
                break

        if component is None:
            raise ValueError('No component named {}'.format(component_name))

        return inspect.getsource(component.__class__)

    def new_graph(self, graph_id):
        """
        Create a new graph.
        """
        self.log.debug('Graph {}: Initializing'.format(graph_id))
        self._graphs[graph_id] = core.Graph(graph_id, initialize=False)

    def add_node(self, graph_id, node_id, component_id):
        """
        Add a component instance.
        """
        # Normally you'd instantiate the component here,
        # we just store the name
        self.log.debug('Graph {}: Adding node {}({})'.format(
            graph_id, component_id, node_id))

        graph = self._create_or_get_graph(graph_id)

        component_class = self._components[component_id]['class']
        component = component_class(node_id)
        graph.add_component(component)

    def remove_node(self, graph_id, node_id):
        """
        Destroy component instance.
        """
        self.log.debug('Graph {}: Removing node {}'.format(
            graph_id, node_id))

        graph = self._create_or_get_graph(graph_id)
        graph.remove_component(node_id)

    def add_edge(self, graph_id, src, tgt):
        """
        Connect ports between components.
        """
        self.log.debug('Graph {}: Connecting ports: {} -> {}'.format(
            graph_id, src, tgt))

        graph = self._graphs[graph_id]

        source_component = self._find_component_by_name(graph, src['node'])
        source_port = source_component.outputs[src['port']]

        target_component = self._find_component_by_name(graph, tgt['node'])
        target_port = target_component.inputs[tgt['port']]

        graph.connect(source_port, target_port)

    def remove_edge(self, graph_id, src, tgt):
        """
        Disconnect ports between components.
        """
        self.log.debug('Graph {}: Disconnecting ports: {} -> {}'.format(
            graph_id, src, tgt))

        graph = self._graphs[graph_id]

        source_component = self._find_component_by_name(graph, src['node'])
        source_port = source_component.outputs[src['port']]
        if source_port.is_connected():
            graph.disconnect(source_port)

        target_component = self._find_component_by_name(graph, tgt['node'])
        target_port = target_component.inputs[tgt['port']]
        if target_port.is_connected():
            graph.disconnect(target_port)

    def add_iip(self, graph_id, src, data):
        """
        Set the inital packet for a component inport.
        """
        self.log.info('Graph {}: Setting IIP to {!r} on port {}'.format(
            graph_id, data, src))

        graph = self._graphs[graph_id]

        target_component = self._find_component_by_name(graph, src['node'])
        target_port = target_component.inputs[src['port']]
        if target_port.is_connected():
            graph.disconnect(target_port)

        graph.set_initial_packet(target_port, data)

    def remove_iip(self, graph_id, src):
        """
        Remove the initial packet for a component inport.
        """
        self.log.debug('Graph {}: Removing IIP from port {}'.format(
            graph_id, src))

        graph = self._graphs[graph_id]

        target_component = self._find_component_by_name(graph, src['node'])
        target_port = target_component.inputs[src['port']]
        if target_port.is_connected():
            graph.disconnect(target_port)

        graph.unset_initial_packet(target_port)


def create_websocket_application(runtime):
    class WebSocketRuntimeAdapterApplication(geventwebsocket.WebSocketApplication):
        """
        Web socket application that hosts a single Runtime.
        """
        def __init__(self, ws):
            super(WebSocketRuntimeAdapterApplication, self).__init__(self)

            self.log = logging.getLogger('%s.%s' % (self.__class__.__module__,
                                                    self.__class__.__name__))

            # if not isinstance(runtime, Runtime):
            #     raise ValueError('runtime must be a Runtime, but was %s' % runtime)

            self.runtime = runtime

        ### WebSocket transport handling ###
        @staticmethod
        def protocol_name():
            """
            WebSocket sub-protocol
            """
            return 'noflo'

        def on_open(self):
            self.log.info("Connection opened")

        def on_close(self, reason):
            self.log.info("Connection closed. Reason: %s" % reason)

        def on_message(self, message, **kwargs):
            self.log.debug('MESSAGE: %s' % message)

            if not message:
                self.log.warn('Got empty message')
                return

            m = json.loads(message)
            dispatch = {
                'runtime': self.handle_runtime,
                'component': self.handle_component,
                'graph': self.handle_graph,
                'network': self.handle_network
            }

            try:
                handler = dispatch[m.get('protocol')]
            except KeyError:
                self.log.warn("Subprotocol '{}' not supported".format(p))
            else:
                handler(m['command'], m['payload'])

        def send(self, protocol, command, payload):
            """
            Send a message to UI/client
            """
            self.ws.send(json.dumps({'protocol': protocol,
                                     'command': command,
                                     'payload': payload}))

        ### Protocol send/responses ###
        def handle_runtime(self, command, payload):
            # Absolute minimum: be able to tell UI info about runtime and supported capabilities
            if command == 'getruntime':
                payload = self.runtime.get_runtime_meta()
                # self.log.debug(json.dumps(payload, indent=4))
                self.send('runtime', 'runtime', payload)

            # network:packet, allows sending data in/out to networks in this runtime
            # can be used to represent the runtime as a FBP component in bigger system "remote subgraph"
            elif command == 'packet':
                # We don't actually run anything, just echo input back and pretend it came from "out"
                payload['port'] = 'out'
                self.send('runtime', 'packet', payload)

            else:
                self.log.warn("Unknown command '%s' for protocol '%s' " % (command, 'runtime'))

        def handle_component(self, command, payload):
            # Practical minimum: be able to tell UI which components are available
            # This allows them to be listed, added, removed and connected together in the UI
            if command == 'list':
                specs = self.runtime.get_all_component_specs()
                for component_name, component_data in specs.iteritems():
                    payload = component_data
                    self.send('component', 'component', payload)

                self.send('component', 'componentsready', None)
            # Get source code for component
            elif command == 'getsource':
                component_name = payload['name']
                source_code = self.runtime.get_source_code(component_name)

                library_name, short_component_name = component_name.split('/', 1)

                payload = {
                    'name': short_component_name,
                    'language': 'python',
                    'library': library_name,
                    'code': source_code,
                    #'tests': ''
                    'secret': payload.get('secret')
                }
                self.send('component', 'source', payload)
            else:
                self.log.warn("Unknown command '%s' for protocol '%s' " % (command, 'component'))

        def handle_graph(self, command, payload):
            # Modify our graph representation to match that of the UI/client
            # Note: if it is possible for the graph state to be changed by other things than the client
            # you must send a message on the same format, informing the client about the change
            # Normally done using signals,observer-pattern or similar

            send_ack = True

            # New graph
            if command == 'clear':
                self.runtime.new_graph(payload['id'])
            # Nodes
            elif command == 'addnode':
                self.runtime.add_node(payload['graph'], payload['id'],
                                      payload['component'])
            elif command == 'removenode':
                self.runtime.remove_node(payload['graph'], payload['id'])
            # Edges/connections
            elif command == 'addedge':
                self.runtime.add_edge(payload['graph'], payload['src'],
                                      payload['tgt'])
            elif command == 'removeedge':
                self.runtime.remove_edge(payload['graph'], payload['src'],
                                         payload['tgt'])
            # IIP / literals
            elif command == 'addinitial':
                self.runtime.add_iip(payload['graph'], payload['tgt'],
                                     payload['src']['data'])
            elif command == 'removeinitial':
                self.runtime.remove_iip(payload['graph'], payload['tgt'])
            # Exported ports
            elif command in ('addinport', 'addoutport'):
                pass  # No support in this example
            # Metadata changes
            elif command in ('changenode',):
                pass
            else:
                send_ack = False
                self.log.warn("Unknown command '%s' for protocol '%s' " % (command, 'graph'))

            # For any message we respected, send same in return as acknowledgement
            if send_ack:
                self.send('graph', command, payload)

        def handle_network(self, command, payload):
            def send_status(cmd, g):
                started = self.runtime.is_started(g)
                # NOTE: running indicates network is actively running, data being processed
                # for this example, we consider ourselves running as long as we have been started
                running = started
                payload = {
                    graph: g,
                    started: started,
                    running: running,
                }
                self.send('network', cmd, payload)

            graph = payload.get('graph', None)
            if command == 'getstatus':
                send_status('status', graph)
            elif command == 'start':
                self.runtime.start(graph)
                send_status('started', graph)
            elif command == 'stop':
                self.runtime.stop(graph)
                send_status('started', graph)
            else:
                self.log.warn("Unknown command '%s' for protocol '%s'" % (command, 'network'))

    return WebSocketRuntimeAdapterApplication


class RuntimeRegistry(object):
    """
    Runtime registry.
    """
    __metaclass__ = ABCMeta

    @abstractmethod
    def register_runtime(self, runtime, runtime_id, user_id, address):
        """
        Registers a runtime.

        :param runtime: the Runtime to register.
        :param user_id: registry user ID.
        :param address: callback address.
        """
        pass

    def ping_runtime(self, runtime_id):
        """
        Pings a registered runtime, keeping it alive in the registry.
        This should be called periodically.

        :param runtime: the Runtime to ping.
        """
        pass


class FlowhubRegistry(RuntimeRegistry):
    """
    FlowHub runtime registry.
    It's necessary to use this if you want to manage your graph in either
    FlowHub or NoFlo-UI.
    """
    def __init__(self):
        self.log = logging.getLogger('%s.%s' % (self.__class__.__module__,
                                                self.__class__.__name__))

        self._endpoint = 'http://api.flowhub.io'

    def register_runtime(self, runtime, runtime_id, user_id, address):
        if not isinstance(runtime, Runtime):
            raise ValueError('runtime must be a Runtime instance')

        runtime_metadata = runtime.get_runtime_meta()
        payload = {
            'id': runtime_id,

            'label': runtime_metadata['label'],
            'type': runtime_metadata['type'],

            'address': address,
            'protocol': 'websocket',

            'user': user_id,
            'secret': '9129923',  # unused
        }

        self.log.info('Registering runtime %s for user %s...' % (runtime_id, user_id))
        response = requests.put('%s/runtimes/%s' % (self._endpoint, runtime_id),
                                data=json.dumps(payload),
                                headers={'Content-type': 'application/json'})
        self._ensure_http_success(response)

    def ping_runtime(self, runtime_id):
        self.log.info('Pinging runtime %s...' % runtime_id)
        response = requests.post('%s/runtimes/%s' % (self._endpoint, runtime_id))
        self._ensure_http_success(response)

    @classmethod
    def _ensure_http_success(cls, response):
        if not (199 < response.status_code < 300):
            raise Exception('Flow API returned error %d: %s' %
                            (response.status_code, response.text))


def create_runtime_id(user_id, address):
    return str(uuid.uuid3(uuid.UUID(user_id), 'pflow_' + address))


def main():
    # Argument defaults
    defaults = {
        'host': 'localhost',
        'port': 3569
    }

    # Parse arguments
    argp = argparse.ArgumentParser(
        description='Runtime that responds to commands sent over the network, '
                    'managing and executing graphs.')
    argp.add_argument(
        '-u', '--user-id', required=True, metavar='UUID',
        help='FlowHub user ID (get this from NoFlo-UI)')
    argp.add_argument(
        '-r', '--runtime-id', metavar='UUID',
        help='FlowHub unique runtime ID (generated if none specified)')
    argp.add_argument(
        '--host', default=defaults['host'], metavar='HOSTNAME',
        help='Listen host for websocket (default: %(host)s)' % defaults)
    argp.add_argument(
        '--port', type=int, default=3569, metavar='PORT',
        help='Listen port for websocket (default: %(port)d)' % defaults)
    argp.add_argument(
        '--log-file', metavar='FILE_PATH',
        help='File to send log output to (default: none)')
    argp.add_argument(
        '-v', '--verbose', action='store_true',
        help='Enable verbose logging')

    # TODO: add arg for executor type (multiprocess, singleprocess, distributed)
    # TODO: add args for component search paths
    args = argp.parse_args()

    # Configure logging
    utils.init_logger(filename=args.log_file,
                      default_level=(logging.DEBUG if args.verbose else logging.INFO),
                      logger_levels={
                          'requests': logging.WARN,
                          'geventwebsocket': logging.INFO,
                          'sh': logging.WARN,

                          'pflow.core': logging.INFO,
                          'pflow.components': logging.INFO,
                          'pflow.executors': logging.INFO
                      })

    address = 'ws://{}:{:d}'.format(args.host, args.port)
    runtime_id = args.runtime_id
    if not runtime_id:
        runtime_id = create_runtime_id(args.user_id, address)
        log.warn('No runtime ID was specified, so one was '
                 'generated: {}'.format(runtime_id))

    runtime = Runtime()
    runtime.register_module(pflow.components)

    def runtime_application_task():
        """
        This greenlet runs the websocket server that responds remote commands
        that inspect/manipulate the Runtime.
        """
        r = geventwebsocket.Resource(OrderedDict([('/', create_websocket_application(runtime))]))
        s = geventwebsocket.WebSocketServer(('', args.port), r)
        s.serve_forever()

    def registration_task():
        """
        This greenlet will register the runtime with FlowHub and occasionally
        ping the endpoint to keep the runtime alive.
        """
        flowhub = FlowhubRegistry()

        # Register runtime
        flowhub.register_runtime(runtime, runtime_id, args.user_id, address)

        # Ping
        delay_secs = 60  # Ping every minute
        while True:
            flowhub.ping_runtime(runtime_id)
            gevent.sleep(delay_secs)

    # Start!
    gevent.wait([
        gevent.spawn(runtime_application_task),
        gevent.spawn(registration_task)
    ])


if __name__ == '__main__':
    main()
