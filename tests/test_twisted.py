import base64
import collections
import json
import struct

import pytest
import pytest_twisted
import six
from twisted.internet import defer, reactor
from twisted.web._newclient import (ResponseNeverReceived,
                                    ResponseFailed,
                                    RequestTransmissionFailed,
                                    _WrapperException)

import consul
import consul.twisted
from consul import ConsulException
from consul.twisted import InsecureContextFactory

Check = consul.Check


@pytest.fixture
def local_server(httpserver):
    from pytest_httpserver import RequestHandler

    handler = httpserver.expect_request('/v1/agent/services')
    assert isinstance(handler, RequestHandler)
    handler.respond_with_data(json.dumps({"foo": "bar"}), status=599)
    port = httpserver.port
    LocalServer = collections.namedtuple('LocalServer', ['port'])
    yield LocalServer(port)
    httpserver.stop()


def sleep(seconds):
    """
    An asynchronous sleep function using twsited. Source:
    http://twistedmatrix.com/pipermail/twisted-python/2009-October/020788.html

    :type seconds: float
    """
    d = defer.Deferred()
    reactor.callLater(seconds, d.callback, seconds)
    return d


class TestConsul(object):
    @pytest_twisted.inlineCallbacks
    def test_kv(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)
        index, data = yield c.kv.get('foo')
        assert data is None
        response = yield c.kv.put('foo', 'bar')
        assert response is True
        index, data = yield c.kv.get('foo')
        assert data['Value'] == six.b('bar')

    @pytest_twisted.inlineCallbacks
    def test_kv_binary(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)
        yield c.kv.put('foo', struct.pack('i', 1000))
        index, data = yield c.kv.get('foo')
        assert struct.unpack('i', data['Value']) == (1000,)

    @pytest_twisted.inlineCallbacks
    def test_kv_missing(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)
        reactor.callLater(2.0 / 100, c.kv.put, 'foo', 'bar')
        yield c.kv.put('index', 'bump')
        index, data = yield c.kv.get('foo')
        assert data is None
        index, data = yield c.kv.get('foo', index=index)
        assert data['Value'] == six.b('bar')

    @pytest_twisted.inlineCallbacks
    def test_kv_put_flags(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)
        yield c.kv.put('foo', 'bar')
        index, data = yield c.kv.get('foo')
        assert data['Flags'] == 0

        response = yield c.kv.put('foo', 'bar', flags=50)
        assert response is True
        index, data = yield c.kv.get('foo')
        assert data['Flags'] == 50

    @pytest_twisted.inlineCallbacks
    def test_kv_delete(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)
        yield c.kv.put('foo1', '1')
        yield c.kv.put('foo2', '2')
        yield c.kv.put('foo3', '3')
        index, data = yield c.kv.get('foo', recurse=True)
        assert [x['Key'] for x in data] == ['foo1', 'foo2', 'foo3']

        response = yield c.kv.delete('foo2')
        assert response is True
        index, data = yield c.kv.get('foo', recurse=True)
        assert [x['Key'] for x in data] == ['foo1', 'foo3']
        response = yield c.kv.delete('foo', recurse=True)
        assert response is True
        index, data = yield c.kv.get('foo', recurse=True)
        assert data is None

    @pytest_twisted.inlineCallbacks
    def test_kv_subscribe(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)

        @defer.inlineCallbacks
        def put():
            response = yield c.kv.put('foo', 'bar')
            assert response is True

        reactor.callLater(1.0 / 100, put)
        index, data = yield c.kv.get('foo')
        assert data is None
        index, data = yield c.kv.get('foo', index=index)
        assert data['Value'] == six.b('bar')

    @pytest_twisted.inlineCallbacks
    def test_transaction(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)
        value = base64.b64encode(b"1").decode("utf8")
        d = {"KV": {"Verb": "set", "Key": "asdf", "Value": value}}
        r = yield c.txn.put([d])
        assert r["Errors"] is None

        d = {"KV": {"Verb": "get", "Key": "asdf"}}
        r = yield c.txn.put([d])
        assert r["Results"][0]["KV"]["Value"] == value

    @pytest_twisted.inlineCallbacks
    def test_agent_services(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)
        services = yield c.agent.services()
        assert services == {}
        response = yield c.agent.service.register('foo')
        assert response is True
        services = yield c.agent.services()
        assert services == {'foo': {'ID': 'foo',
                                    'Service': 'foo',
                                    'Tags': [],
                                    'Meta': {},
                                    'Port': 0,
                                    'Address': '',
                                    'Weights': {'Passing': 1, 'Warning': 1},
                                    'EnableTagOverride': False
                                    }
                            }
        response = yield c.agent.service.deregister('foo')
        assert response is True
        services = yield c.agent.services()
        assert services == {}

    @pytest_twisted.inlineCallbacks
    def test_catalog(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)

        @defer.inlineCallbacks
        def register():
            response = yield c.catalog.register('n1', '10.1.10.11')
            assert response is True
            yield sleep(50 / 1000.0)
            response = yield c.catalog.deregister('n1')
            assert response is True

        reactor.callLater(1.0 / 100, register)

        index, nodes = yield c.catalog.nodes()
        assert len(nodes) == 1
        current = nodes[0]

        index, nodes = yield c.catalog.nodes(index=index)
        nodes.remove(current)
        assert [x['Node'] for x in nodes] == ['n1']

        index, nodes = yield c.catalog.nodes(index=index)
        nodes.remove(current)
        assert [x['Node'] for x in nodes] == []

    @pytest_twisted.inlineCallbacks
    def test_health_service(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)

        # check there are no nodes for the service 'foo'
        index, nodes = yield c.health.service('foo')
        assert nodes == []

        # register two nodes, one with a long ttl, the other shorter
        yield c.agent.service.register(
            'foo', service_id='foo:1', check=Check.ttl('10s'))
        yield c.agent.service.register(
            'foo', service_id='foo:2', check=Check.ttl('100ms'))

        yield sleep(1.0)

        # check the nodes show for the /health/service endpoint
        index, nodes = yield c.health.service('foo')
        assert [node['Service']['ID'] for node in nodes] == \
               ['foo:1', 'foo:2']

        # but that they aren't passing their health check
        index, nodes = yield c.health.service('foo', passing=True)
        assert nodes == []

        # ping the two node's health check
        yield c.agent.check.ttl_pass('service:foo:1')
        yield c.agent.check.ttl_pass('service:foo:2')

        yield sleep(0.05)

        # both nodes are now available
        index, nodes = yield c.health.service('foo', passing=True)
        assert [node['Service']['ID'] for node in nodes] == \
               ['foo:1', 'foo:2']

        # wait until the short ttl node fails
        yield sleep(0.5)

        # only one node available
        index, nodes = yield c.health.service('foo', passing=True)
        assert [node['Service']['ID'] for node in nodes] == ['foo:1']

        # ping the failed node's health check
        yield c.agent.check.ttl_pass('service:foo:2')

        yield sleep(0.05)

        # check both nodes are available
        index, nodes = yield c.health.service('foo', passing=True)
        assert [node['Service']['ID'] for node in nodes] == \
               ['foo:1', 'foo:2']

        # deregister the nodes
        yield c.agent.service.deregister('foo:1')
        yield c.agent.service.deregister('foo:2')

        yield sleep(2)
        index, nodes = yield c.health.service('foo')
        assert nodes == []

    @pytest_twisted.inlineCallbacks
    def test_health_service_subscribe(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)

        class Config(object):
            def __init__(self):
                self.nodes = []
                self.index = None

            @defer.inlineCallbacks
            def update(self):
                self.index, nodes = yield c.health.service(
                    'foo', index=None, passing=True)
                self.nodes = [node['Service']['ID'] for node in nodes]

        config = Config()
        yield c.agent.service.register(
            'foo', service_id='foo:1', check=Check.ttl('40ms'))
        yield config.update()
        assert config.nodes == []

        # ping the service's health check
        yield c.agent.check.ttl_pass('service:foo:1')
        yield config.update()
        assert config.nodes == ['foo:1']

        # the service should fail
        yield sleep(0.8)
        yield config.update()
        assert config.nodes == []

        yield c.agent.service.deregister('foo:1')

    @pytest_twisted.inlineCallbacks
    def test_session(self, consul_port):
        c = consul.twisted.Consul(port=consul_port)

        index, services = yield c.session.list()
        assert services == []

        session_id = yield c.session.create()
        index, services = yield c.session.list(index=index)
        assert len(services)

        response = yield c.session.destroy(session_id)
        assert response is True

        index, services = yield c.session.list(index=index)
        assert services == []

    def test_get_content(self):

        content = InsecureContextFactory().getContext('localhost', '8000')
        assert content.get_app_data() is None

    @pytest_twisted.inlineCallbacks
    def test_virify(self, consul_port):
        c = consul.twisted.Consul(port=consul_port, verify=False)

        index, services = yield c.session.list()
        assert services == []

    @pytest_twisted.inlineCallbacks
    def test_context(self, consul_port):
        c = consul.twisted.Consul(port=0, verify=False)
        except_consul = False
        try:
            yield c.session.list()
        except AttributeError:
            except_consul = True
        assert except_consul

        c = consul.twisted.Consul(port=consul_port,
                                  contextFactory=InsecureContextFactory)
        sess = yield c.agent.services()
        assert sess == {}

    @pytest_twisted.inlineCallbacks
    def test_http_compat_string(self, consul_port, local_server):
        c = consul.twisted.Consul(port=consul_port, verify=False)
        yield c.agent.services()
        compat_string = "foo"
        assert compat_string == c.http.compat_string(compat_string)

    @pytest_twisted.inlineCallbacks
    def test_gen_exception(self, consul_port, local_server):
        c = consul.twisted.Consul(port=consul_port, verify=False)
        yield c.agent.services()

        def function_response_never_received(args):
            raise ResponseNeverReceived(ResponseFailed)

        def function_request_transmission_failed(args):
            raise RequestTransmissionFailed(_WrapperException)

        try:
            yield c.http.request(function_response_never_received,
                                 "head",
                                 "http://127.0.0.1:" + str(local_server.port))
        except Exception as e:
            assert isinstance(e, ConsulException)

        try:
            yield c.http.request(function_request_transmission_failed, "head",
                                 "http://127.0.0.1:" + str(local_server.port))
        except Exception as e:
            assert isinstance(e, ConsulException)
