
import urllib, simplejson

from twisted.internet import defer, reactor

from twisted.web.client import Agent
from twisted.web.http import UNAUTHORIZED

from zope.interface import implements, Interface

from allmydata.util import log
from allmydata.node import InvalidValueError
from allmydata.storage.backends.base import ContainerItem, ContainerListing
from allmydata.storage.backends.cloud.cloud_common import IContainer, \
     CloudServiceError, CommonContainerMixin, HTTPClientMixin


# Enabling this will cause secrets to be logged.
UNSAFE_DEBUG = False


DEFAULT_AUTH_URLS = {
    "rackspace.com v1": "https://identity.api.rackspacecloud.com/v1.0",
    "rackspace.co.uk v1": "https://lon.identity.api.rackspacecloud.com/v1.0",
    "rackspace.com": "https://identity.api.rackspacecloud.com/v2.0/tokens",
    "rackspace.co.uk": "https://lon.identity.api.rackspacecloud.com/v2.0/tokens",
    "hpcloud.com west": "https://region-a.geo-1.identity.hpcloudsvc.com:35357/v2.0/tokens",
    "hpcloud.com east": "https://region-b.geo-1.identity.hpcloudsvc.com:35357/v2.0/tokens",
}


def configure_openstack_container(storedir, config):
    provider = config.get_config("storage", "openstack.provider", "rackspace.com").lower()
    if provider not in DEFAULT_AUTH_URLS:
        raise InvalidValueError("[storage]openstack.provider %r is not recognized\n"
                                "Valid providers are: %s" % (provider, ", ".join(sorted(DEFAULT_AUTH_URLS.keys()))))

    auth_service_url = config.get_config("storage", "openstack.url", DEFAULT_AUTH_URLS[provider])
    container_name = config.get_config("storage", "openstack.container")
    reauth_period = 11*60*60 #seconds

    access_key_id = config.get_config("storage", "openstack.access_key_id", None)
    if access_key_id is None:
        username = config.get_config("storage", "openstack.username")
        api_key = config.get_private_config("openstack_api_key")
        if auth_service_url.endswith("/v1.0"):
            authenticator = AuthenticatorV1(auth_service_url, username, api_key)
        else:
            authenticator = AuthenticatorV2(auth_service_url, {
              'RAX-KSKEY:apiKeyCredentials': {
                'username': username,
                'apiKey': api_key,
              }
            })
    else:
        tenant_id = config.get_config("storage", "openstack.tenant_id")
        secret_key = config.get_private_config("openstack_secret_key")
        authenticator = AuthenticatorV2(auth_service_url, {
          'apiAccessKeyCredentials': {
            'accessKey': access_key_id,
            'secretKey': secret_key,
          },
          'tenantId': tenant_id,
        })

    auth_client = AuthenticationClient(authenticator, reauth_period)
    return OpenStackContainer(auth_client, container_name)


class AuthenticationInfo(object):
    def __init__(self, auth_token, public_storage_url, internal_storage_url=None):
        self.auth_token = auth_token
        self.public_storage_url = public_storage_url
        self.internal_storage_url = internal_storage_url


class IAuthenticator(Interface):
    def make_auth_request():
        """Returns (method, url, headers, body, need_response_body)."""

    def parse_auth_response(response, get_header, body):
        """Returns AuthenticationInfo."""


class AuthenticatorV1(object):
    implements(IAuthenticator)
    """
    Authenticates according to V1 protocol as documented by Rackspace:
    <http://docs.rackspace.com/files/api/v1/cf-devguide/content/Authentication-d1e639.html>.
    """

    def __init__(self, auth_service_url, username, api_key):
        self._auth_service_url = auth_service_url
        self._username = username
        self._api_key = api_key

    def make_auth_request(self):
        request_headers = {
            'X-Auth-User': [self._username],
            'X-Auth-Key': [self._api_key],
        }
        return ('GET', self._auth_service_url, request_headers, None, False)

    def parse_auth_response(self, response, get_header, body):
        auth_token = get_header(response, 'X-Auth-Token')
        storage_url = get_header(response, 'X-Storage-Url')
        #cdn_management_url = get_header(response, 'X-CDN-Management-Url')
        return AuthenticationInfo(auth_token, storage_url)


class AuthenticatorV2(object):
    implements(IAuthenticator)
    """
    Authenticates according to V2 protocol as documented by Rackspace:
    <http://docs.rackspace.com/auth/api/v2.0/auth-client-devguide/content/POST_authenticate_v2.0_tokens_.html>.

    This is also compatible with HP's protocol (using different credentials):
    <https://docs.hpcloud.com/api/identity#authenticate-jumplink-span>.
    """

    def __init__(self, auth_service_url, credentials):
        self._auth_service_url = auth_service_url
        self._credentials = credentials

    def make_auth_request(self):
        request = {'auth': self._credentials}
        json = simplejson.dumps(request)
        request_headers = {
            'Content-Type': ['application/json'],
        }
        return ('POST', self._auth_service_url, request_headers, json, True)

    def parse_auth_response(self, response, get_header, body):
        try:
            decoded_body = simplejson.loads(body)
        except simplejson.decoder.JSONDecodeError, e:
            raise CloudServiceError(None, response.code,
                                    message="could not decode auth response: %s" % (e,))

        try:
            # Scrabble around in the annoyingly complicated response body for the credentials we need.
            access = decoded_body['access']
            token = access['token']
            auth_token = token['id']

            user = access['user']
            default_region = user.get('RAX-AUTH:defaultRegion', '')

            serviceCatalog = access['serviceCatalog']
            for service in serviceCatalog:
                if service['type'] == 'object-store':
                    endpoints = service['endpoints']
                    for endpoint in endpoints:
                        if not default_region or endpoint['region'] == default_region:
                            public_storage_url = endpoint['publicURL']
                            internal_storage_url = endpoint.get('internalURL', None)
                            return AuthenticationInfo(auth_token, public_storage_url, internal_storage_url)
        except KeyError, e:
            raise CloudServiceError(None, response.code,
                                    message="missing field in auth response: %s" % (e,))

        raise CloudServiceError(None, response.code,
                                message="could not find a suitable storage endpoint in auth response")


class AuthenticationClient(HTTPClientMixin):
    """
    I implement a generic authentication client.
    The construction of the auth request and parsing of the response is delegated to an authenticator.
    """

    USER_AGENT = "Tahoe-LAFS OpenStack authentication client"

    def __init__(self, authenticator, reauth_period, override_reactor=None):
        self._authenticator = authenticator
        self._reauth_period = reauth_period
        self._reactor = override_reactor or reactor
        self._agent = Agent(self._reactor)
        self._shutdown = False
        self.ServiceError = CloudServiceError

        # Not authorized yet.
        self._auth_info = None
        self._auth_lock = defer.DeferredLock()
        self._reauthenticate()

    def get_auth_info(self):
        # It is intentional that this returns the previous auth_info while a reauthentication is in progress.
        if self._auth_info is not None:
            return defer.succeed(self._auth_info)
        else:
            return self.get_auth_info_locked()

    def get_auth_info_locked(self):
        d = self._auth_lock.run(self._authenticate)
        d.addCallback(lambda ign: self._auth_info)
        return d

    def invalidate(self):
        self._auth_info = None
        self._reauthenticate()

    def _authenticate(self):
        (method, url, request_headers, body, need_response_body) = self._authenticator.make_auth_request()

        d = self._http_request("OpenStack auth", method, url, request_headers, body, need_response_body)
        def _got_response( (response, body) ):
            self._auth_info = self._authenticator.parse_auth_response(response, self._get_header, body)
            if UNSAFE_DEBUG:
                print "Auth response is %s %s" % (self._auth_info.auth_token, self._auth_info.public_storage_url)

            if not self._shutdown:
                if self._delayed:
                    self._delayed.cancel()
                self._delayed = self._reactor.callLater(self._reauth_period, self._reauthenticate)
        d.addCallback(_got_response)
        def _failed(f):
            self._auth_info = None
            # do we need to retry?
            log.err(f)
            return f
        d.addErrback(_failed)
        return d

    def _reauthenticate(self):
        self._delayed = None
        d = self.get_auth_info_locked()
        d.addBoth(lambda ign: None)
        return d

    def shutdown(self):
        """Used by unit tests to avoid unclean reactor errors."""
        self._shutdown = True
        if self._delayed:
            self._delayed.cancel()


class OpenStackContainer(CommonContainerMixin, HTTPClientMixin):
    implements(IContainer)

    USER_AGENT = "Tahoe-LAFS OpenStack storage client"

    def __init__(self, auth_client, container_name, override_reactor=None):
        CommonContainerMixin.__init__(self, container_name, override_reactor)
        self._init_agent()
        self._auth_client = auth_client

    def _react_to_error(self, response_code):
        if response_code == UNAUTHORIZED:
            # Invalidate auth_info and retry.
            self._auth_client.invalidate()
            return True
        else:
            return CommonContainerMixin._react_to_error(self, response_code)

    def _create(self):
        """
        Create this container.
        """
        raise NotImplementedError

    def _delete(self):
        """
        Delete this container.
        The cloud service may require the container to be empty before it can be deleted.
        """
        raise NotImplementedError

    def _list_objects(self, prefix=''):
        """
        Get a ContainerListing that lists objects in this container.

        prefix: (str) limit the returned keys to those starting with prefix.
        """
        d = self._auth_client.get_auth_info()
        def _do_list(auth_info):
            request_headers = {
                'X-Auth-Token': [auth_info.auth_token],
            }
            url = self._make_container_url(auth_info.public_storage_url)
            if prefix:
                url += "?format=json&prefix=%s" % (urllib.quote(prefix, safe=''),)
            return self._http_request("OpenStack list objects", 'GET', url, request_headers,
                                      need_response_body=True)
        d.addCallback(_do_list)
        d.addCallback(lambda (response, json): self._parse_list(response, json, prefix))
        return d

    def _parse_list(self, response, json, prefix):
        try:
            items = simplejson.loads(json)
        except simplejson.decoder.JSONDecodeError, e:
            raise self.ServiceError(None, response.code,
                                    message="could not decode list response: %s" % (e,))

        log.msg(format="OpenStack list read %(length)d bytes, parsed as %(items)d items",
                length=len(json), items=len(items), level=log.OPERATIONAL)

        def _make_containeritem(item):
            try:
                key = item['name']
                size = item['bytes']
                modification_date = item['last_modified']
                etag = item['hash']
                storage_class = 'STANDARD'
            except KeyError, e:
                raise self.ServiceError(None, response.code,
                                        message="missing field in list response: %s" % (e,))
            else:
                return ContainerItem(key, modification_date, etag, size, storage_class)

        contents = map(_make_containeritem, items)
        return ContainerListing(self._container_name, prefix, None, 10000, "false", contents=contents)

    def _put_object(self, object_name, data, content_type='application/octet-stream', metadata={}):
        """
        Put an object in this bucket.
        Any existing object of the same name will be replaced.
        """
        d = self._auth_client.get_auth_info()
        def _do_put(auth_info):
            request_headers = {
                'X-Auth-Token': [auth_info.auth_token],
                'Content-Type': [content_type],
            }
            url = self._make_object_url(auth_info.public_storage_url, object_name)
            return self._http_request("OpenStack put object", 'PUT', url, request_headers, data)
        d.addCallback(_do_put)
        d.addCallback(lambda ign: None)
        return d

    def _get_object(self, object_name):
        """
        Get an object from this container.
        """
        d = self._auth_client.get_auth_info()
        def _do_get(auth_info):
            request_headers = {
                'X-Auth-Token': [auth_info.auth_token],
            }
            url = self._make_object_url(auth_info.public_storage_url, object_name)
            return self._http_request("OpenStack get object", 'GET', url, request_headers,
                                      need_response_body=True)
        d.addCallback(_do_get)
        d.addCallback(lambda (response, body): body)
        return d

    def _head_object(self, object_name):
        """
        Retrieve object metadata only.
        """
        d = self._auth_client.get_auth_info()
        def _do_head(auth_info):
            request_headers = {
                'X-Auth-Token': [auth_info.auth_token],
            }
            url = self._make_object_url(auth_info.public_storage_url, object_name)
            return self._http_request("OpenStack head object", 'HEAD', url, request_headers)
        d.addCallback(_do_head)
        def _got_head_response( (response, body) ):
            print response
            raise NotImplementedError
        d.addCallback(_got_head_response)
        return d

    def _delete_object(self, object_name):
        """
        Delete an object from this container.
        Once deleted, there is no method to restore or undelete an object.
        """
        d = self._auth_client.get_auth_info()
        def _do_delete(auth_info):
            request_headers = {
                'X-Auth-Token': [auth_info.auth_token],
            }
            url = self._make_object_url(auth_info.public_storage_url, object_name)
            return self._http_request("OpenStack delete object", 'DELETE', url, request_headers)
        d.addCallback(_do_delete)
        d.addCallback(lambda ign: None)
        return d
