import os, sys

import twisted
from twisted.trial import unittest
from twisted.application import service

import allmydata
import allmydata.frontends.magic_folder
import allmydata.util.log

from allmydata.node import Node, OldConfigError, OldConfigOptionError, InvalidValueError, \
     MissingConfigEntry, UnescapedHashError
from allmydata.frontends.auth import NeedRootcapLookupScheme
from allmydata import client
from allmydata.storage_client import StorageFarmBroker
from allmydata.storage.backends.disk.disk_backend import DiskBackend
from allmydata.storage.backends.cloud.cloud_backend import CloudBackend
from allmydata.util import base32, fileutil
from allmydata.interfaces import IFilesystemNode, IFileNode, \
     IImmutableFileNode, IMutableFileNode, IDirectoryNode
from foolscap.api import flushEventualQueue
import allmydata.test.common_util as testutil

import mock


BASECONFIG = ("[client]\n"
              "introducer.furl = \n"
              )

BASECONFIG_I = ("[client]\n"
              "introducer.furl = %s\n"
              )

class Basic(testutil.ReallyEqualMixin, testutil.NonASCIIPathMixin, unittest.TestCase):
    def test_loadable(self):
        basedir = "test_client.Basic.test_loadable"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG)
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        self.failUnless(isinstance(server.backend, DiskBackend), server.backend)

    def test_comment(self):
        should_fail = [r"test#test", r"#testtest", r"test\\#test"]
        should_not_fail = [r"test\#test", r"test\\\#test", r"testtest"]

        basedir = "test_client.Basic.test_comment"
        os.mkdir(basedir)

        def write_config(s):
            config = ("[client]\n"
                      "introducer.furl = %s\n" % s)
            fileutil.write(os.path.join(basedir, "tahoe.cfg"), config)

        for s in should_fail:
            self.failUnless(Node._contains_unescaped_hash(s))
            write_config(s)
            e = self.assertRaises(UnescapedHashError, client.Client, basedir)
            self.assertIn("[client]introducer.furl", str(e))

        for s in should_not_fail:
            self.failIf(Node._contains_unescaped_hash(s))
            write_config(s)
            client.Client(basedir)

    def test_unreadable_config(self):
        if sys.platform == "win32":
            # if somebody knows a clever way to do this (cause
            # EnvironmentError when reading a file that really exists), on
            # windows, please fix this
            raise unittest.SkipTest("can't make unreadable files on windows")
        basedir = "test_client.Basic.test_unreadable_config"
        os.mkdir(basedir)
        fn = os.path.join(basedir, "tahoe.cfg")
        fileutil.write(fn, BASECONFIG)
        old_mode = os.stat(fn).st_mode
        os.chmod(fn, 0)
        try:
            e = self.assertRaises(EnvironmentError, client.Client, basedir)
            self.assertIn("Permission denied", str(e))
        finally:
            # don't leave undeleteable junk lying around
            os.chmod(fn, old_mode)

    def test_error_on_old_config_files(self):
        basedir = "test_client.Basic.test_error_on_old_config_files"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       BASECONFIG +
                       "[storage]\n" +
                       "enabled = false\n" +
                       "reserved_space = bogus\n")
        fileutil.write(os.path.join(basedir, "introducer.furl"), "")
        fileutil.write(os.path.join(basedir, "no_storage"), "")
        fileutil.write(os.path.join(basedir, "readonly_storage"), "")
        fileutil.write(os.path.join(basedir, "debug_discard_storage"), "")

        logged_messages = []
        self.patch(twisted.python.log, 'msg', logged_messages.append)

        e = self.failUnlessRaises(OldConfigError, client.Client, basedir)
        abs_basedir = fileutil.abspath_expanduser_unicode(unicode(basedir)).encode(sys.getfilesystemencoding())
        self.failUnlessIn(os.path.join(abs_basedir, "introducer.furl"), e.args[0])
        self.failUnlessIn(os.path.join(abs_basedir, "no_storage"), e.args[0])
        self.failUnlessIn(os.path.join(abs_basedir, "readonly_storage"), e.args[0])
        self.failUnlessIn(os.path.join(abs_basedir, "debug_discard_storage"), e.args[0])

        for oldfile in ['introducer.furl', 'no_storage', 'readonly_storage',
                        'debug_discard_storage']:
            logged = [ m for m in logged_messages if
                       ("Found pre-Tahoe-LAFS-v1.3 configuration file" in str(m) and oldfile in str(m)) ]
            self.failUnless(logged, (oldfile, logged_messages))

        for oldfile in [
            'nickname', 'webport', 'keepalive_timeout', 'log_gatherer.furl',
            'disconnect_timeout', 'advertised_ip_addresses', 'helper.furl',
            'key_generator.furl', 'stats_gatherer.furl', 'sizelimit',
            'run_helper']:
            logged = [ m for m in logged_messages if
                       ("Found pre-Tahoe-LAFS-v1.3 configuration file" in str(m) and oldfile in str(m)) ]
            self.failIf(logged, (oldfile, logged_messages))

    def test_secrets(self):
        basedir = "test_client.Basic.test_secrets"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG)
        c = client.Client(basedir)
        secret_fname = os.path.join(basedir, "private", "secret")
        self.failUnless(os.path.exists(secret_fname), secret_fname)
        renew_secret = c.get_renewal_secret()
        self.failUnless(base32.b2a(renew_secret))
        cancel_secret = c.get_cancel_secret()
        self.failUnless(base32.b2a(cancel_secret))

    def test_nodekey_yes_storage(self):
        basedir = "test_client.Basic.test_nodekey_yes_storage"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       BASECONFIG)
        c = client.Client(basedir)
        self.failUnless(c.get_long_nodeid().startswith("v0-"))

    def test_nodekey_no_storage(self):
        basedir = "test_client.Basic.test_nodekey_no_storage"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       BASECONFIG + "[storage]\n" + "enabled = false\n")
        c = client.Client(basedir)
        self.failUnless(c.get_long_nodeid().startswith("v0-"))

    def test_reserved_1(self):
        basedir = "client.Basic.test_reserved_1"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "reserved_space = 1000\n")
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        self.failUnlessReallyEqual(server.backend._reserved_space, 1000)

    def test_reserved_2(self):
        basedir = "client.Basic.test_reserved_2"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "reserved_space = 10K\n")
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        self.failUnlessReallyEqual(server.backend._reserved_space, 10*1000)

    def test_reserved_3(self):
        basedir = "client.Basic.test_reserved_3"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "reserved_space = 5mB\n")
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        self.failUnlessReallyEqual(server.backend._reserved_space, 5*1000*1000)

    def test_reserved_4(self):
        basedir = "client.Basic.test_reserved_4"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "reserved_space = 78Gb\n")
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        self.failUnlessReallyEqual(server.backend._reserved_space, 78*1000*1000*1000)

    def test_reserved_default(self):
        # This is testing the default when 'reserved_space' is not present, not
        # the default for a newly created node.
        basedir = "client.Basic.test_reserved_default"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n")
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        self.failUnlessReallyEqual(server.backend._reserved_space, 0)

    def test_reserved_bad(self):
        basedir = "client.Basic.test_reserved_bad"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "reserved_space = bogus\n")
        self.failUnlessRaises(InvalidValueError, client.Client, basedir)

    def _write_secret(self, basedir, filename, secret="dummy"):
        fileutil.make_dirs(os.path.join(basedir, "private"))
        fileutil.write(os.path.join(basedir, "private", filename), secret)

    @mock.patch('allmydata.storage.backends.cloud.s3.s3_container.S3Container')
    def test_s3_config_good_defaults(self, mock_S3Container):
        basedir = "client.Basic.test_s3_config_good_defaults"
        os.mkdir(basedir)
        self._write_secret(basedir, "s3secret")
        config = (BASECONFIG +
                  "[storage]\n" +
                  "enabled = true\n" +
                  "backend = cloud.s3\n" +
                  "s3.access_key_id = keyid\n" +
                  "s3.bucket = test\n")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), config)

        c = client.Client(basedir)
        mock_S3Container.assert_called_with("keyid", "dummy", "http://s3.amazonaws.com", "test", None, None)
        server = c.getServiceNamed("storage")
        self.failUnless(isinstance(server.backend, CloudBackend), server.backend)

        mock_S3Container.reset_mock()
        self._write_secret(basedir, "s3producttoken", secret="{ProductToken}")
        self.failUnlessRaises(InvalidValueError, client.Client, basedir)

        mock_S3Container.reset_mock()
        self._write_secret(basedir, "s3usertoken", secret="{UserToken}")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), config + "s3.url = http://s3.example.com\n")

        c = client.Client(basedir)
        mock_S3Container.assert_called_with("keyid", "dummy", "http://s3.example.com", "test",
                                            "{UserToken}", "{ProductToken}")

    def test_s3_readonly_bad(self):
        basedir = "client.Basic.test_s3_readonly_bad"
        os.mkdir(basedir)
        self._write_secret(basedir, "s3secret")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "readonly = true\n" +
                                    "backend = cloud.s3\n" +
                                    "s3.access_key_id = keyid\n" +
                                    "s3.bucket = test\n")
        self.failUnlessRaises(InvalidValueError, client.Client, basedir)

    def test_s3_config_no_access_key_id(self):
        basedir = "client.Basic.test_s3_config_no_access_key_id"
        os.mkdir(basedir)
        self._write_secret(basedir, "s3secret")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "backend = cloud.s3\n" +
                                    "s3.bucket = test\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    def test_s3_config_no_bucket(self):
        basedir = "client.Basic.test_s3_config_no_bucket"
        os.mkdir(basedir)
        self._write_secret(basedir, "s3secret")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "backend = cloud.s3\n" +
                                    "s3.access_key_id = keyid\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    def test_s3_config_no_s3secret(self):
        basedir = "client.Basic.test_s3_config_no_s3secret"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "backend = cloud.s3\n" +
                                    "s3.access_key_id = keyid\n" +
                                    "s3.bucket = test\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    @mock.patch('allmydata.storage.backends.cloud.openstack.openstack_container.AuthenticatorV2')
    @mock.patch('allmydata.storage.backends.cloud.openstack.openstack_container.AuthenticationClient')
    @mock.patch('allmydata.storage.backends.cloud.openstack.openstack_container.OpenStackContainer')
    def test_openstack_config_good_defaults(self, mock_OpenStackContainer, mock_AuthenticationClient,
                                            mock_Authenticator):
        basedir = "client.Basic.test_openstack_config_good_defaults"
        os.mkdir(basedir)
        self._write_secret(basedir, "openstack_api_key")
        config = (BASECONFIG +
                  "[storage]\n" +
                  "enabled = true\n" +
                  "backend = cloud.openstack\n" +
                  "openstack.provider = rackspace.com\n" +
                  "openstack.username = alex\n" +
                  "openstack.container = test\n")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), config)

        c = client.Client(basedir)
        mock_Authenticator.assert_called_with("https://identity.api.rackspacecloud.com/v2.0/tokens",
                                              {'RAX-KSKEY:apiKeyCredentials': {'username': 'alex', 'apiKey': 'dummy'}})
        authclient_call_args = mock_AuthenticationClient.call_args_list
        self.failUnlessEqual(len(authclient_call_args), 1)
        self.failUnlessEqual(authclient_call_args[0][0][1:], (11*60*60,))
        container_call_args = mock_OpenStackContainer.call_args_list
        self.failUnlessEqual(len(container_call_args), 1)
        self.failUnlessEqual(container_call_args[0][0][1:], ("test",))
        server = c.getServiceNamed("storage")
        self.failUnless(isinstance(server.backend, CloudBackend), server.backend)

    def test_openstack_readonly_bad(self):
        basedir = "client.Basic.test_openstack_readonly_bad"
        os.mkdir(basedir)
        self._write_secret(basedir, "openstack_api_key")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "readonly = true\n" +
                                    "backend = cloud.openstack\n" +
                                    "openstack.provider = rackspace.com\n" +
                                    "openstack.username = alex\n" +
                                    "openstack.container = test\n")
        self.failUnlessRaises(InvalidValueError, client.Client, basedir)

    def test_openstack_config_no_username(self):
        basedir = "client.Basic.test_openstack_config_no_username"
        os.mkdir(basedir)
        self._write_secret(basedir, "openstack_api_key")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "backend = cloud.openstack\n" +
                                    "openstack.provider = rackspace.com\n" +
                                    "openstack.container = test\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    def test_openstack_config_no_container(self):
        basedir = "client.Basic.test_openstack_config_no_container"
        os.mkdir(basedir)
        self._write_secret(basedir, "openstack_api_key")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "backend = cloud.openstack\n" +
                                    "openstack.provider = rackspace.com\n" +
                                    "openstack.username = alex\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    def test_openstack_config_no_api_key(self):
        basedir = "client.Basic.test_openstack_config_no_api_key"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                                    BASECONFIG +
                                    "[storage]\n" +
                                    "enabled = true\n" +
                                    "backend = cloud.openstack\n" +
                                    "openstack.provider = rackspace.com\n" +
                                    "openstack.username = alex\n" +
                                    "openstack.container = test\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    def test_googlestorage_config_required(self):
        """
        account_email, bucket and project_id are all required by
        googlestorage configuration.
        """
        configs = ["googlestorage.account_email = u@example.com",
                   "googlestorage.bucket = bucket",
                   "googlestorage.project_id = 456"]
        for i in range(len(configs)):
            basedir = self.mktemp()
            os.mkdir(basedir)
            bad_config = configs[:]
            del bad_config[i]
            self._write_secret(basedir, "googlestorage_private_key")
            fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                           BASECONFIG +
                           "[storage]\n" +
                           "enabled = true\n" +
                           "backend = cloud.googlestorage\n" +
                           "\n".join(bad_config) + "\n")
            self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    def test_googlestorage_config_required_private_key(self):
        """
        googlestorage_private_key secret is required by googlestorage
        configuration.
        """
        basedir = self.mktemp()
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       BASECONFIG +
                       "[storage]\n" +
                       "enabled = true\n" +
                       "backend = cloud.googlestorage\n" +
                       "googlestorage.account_email = u@example.com\n" +
                       "googlestorage.bucket = bucket\n" +
                       "googlestorage.project_id = 456\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    @mock.patch('allmydata.storage.backends.cloud.googlestorage.googlestorage_container.AuthenticationClient')
    @mock.patch('allmydata.storage.backends.cloud.googlestorage.googlestorage_container.GoogleStorageContainer')
    def test_googlestorage_config(self, mock_OpenStackContainer, mock_AuthenticationClient):
        """
        Given good configuration, we correctly configure a good GoogleStorageContainer.
        """
        basedir = self.mktemp()
        os.mkdir(basedir)
        self._write_secret(basedir, "googlestorage_private_key", "sekrit")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       BASECONFIG +
                       "[storage]\n" +
                       "enabled = true\n" +
                       "backend = cloud.googlestorage\n" +
                       "googlestorage.account_email = u@example.com\n" +
                       "googlestorage.bucket = bucket\n" +
                       "googlestorage.project_id = 456\n")
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        self.failUnless(isinstance(server.backend, CloudBackend), server.backend)
        # Protect against typos with isinstance(), because mock is dangerous.
        self.assertFalse(isinstance(mock_AuthenticationClient.assert_called_once_with,
                                    mock.Mock))
        mock_AuthenticationClient.assert_called_once_with("u@example.com", "sekrit")
        self.assertFalse(isinstance(mock_OpenStackContainer.assert_called_once_with,
                                    mock.Mock))
        mock_OpenStackContainer.assert_called_once_with(mock_AuthenticationClient.return_value,
                                                        "456", "bucket")

    def test_msazure_config_required(self):
        """
        account_name and container are all required by MS Azure configuration.
        """
        configs = ["mszure.account_name = theaccount",
                   "msazure.container = bucket"]
        for i in range(len(configs)):
            basedir = self.mktemp()
            os.mkdir(basedir)
            bad_config = configs[:]
            del bad_config[i]
            self._write_secret(basedir, "msazure_account_key")
            fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                           BASECONFIG +
                           "[storage]\n" +
                           "enabled = true\n" +
                           "backend = cloud.msazure\n" +
                           "\n".join(bad_config) + "\n")
            self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    def test_msazure_config_required_private_key(self):
        """
        msazure_account_key secret is required by MS Azure configuration.
        """
        basedir = self.mktemp()
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       BASECONFIG +
                       "[storage]\n" +
                       "enabled = true\n" +
                       "backend = cloud.msazure\n" +
                       "msazure.account_name = theaccount\n" +
                       "msazure.container = bucket\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir)

    @mock.patch('allmydata.storage.backends.cloud.msazure.msazure_container.MSAzureStorageContainer')
    def test_msazure_config(self, mock_MSAzureStorageContainer):
        """
        Given good configuration, we correctly configure a good MSAzureStorageContainer.
        """
        basedir = self.mktemp()
        os.mkdir(basedir)
        self._write_secret(basedir, "msazure_account_key", "abc")
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       BASECONFIG +
                       "[storage]\n" +
                       "enabled = true\n" +
                       "backend = cloud.msazure\n" +
                       "msazure.account_name = theaccount\n" +
                       "msazure.container = bucket\n")
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        self.failUnless(isinstance(server.backend, CloudBackend), server.backend)
        # Protect against typos with isinstance(), because mock is dangerous.
        self.assertFalse(isinstance(
                mock_MSAzureStorageContainer.assert_called_once_with, mock.Mock))
        mock_MSAzureStorageContainer.assert_called_once_with(
            "theaccount", "abc", "bucket")

    def test_expire_mutable_false_unsupported(self):
        basedir = "client.Basic.test_expire_mutable_false_unsupported"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), \
                       BASECONFIG + \
                       "[storage]\n" + \
                       "enabled = true\n" + \
                       "expire.mutable = False\n")
        self.failUnlessRaises(OldConfigOptionError, client.Client, basedir)

    def test_expire_immutable_false_unsupported(self):
        basedir = "client.Basic.test_expire_immutable_false_unsupported"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), \
                       BASECONFIG + \
                       "[storage]\n" + \
                       "enabled = true\n" + \
                       "expire.immutable = False\n")
        self.failUnlessRaises(OldConfigOptionError, client.Client, basedir)

    def test_debug_discard_true_unsupported(self):
        basedir = "client.Basic.test_debug_discard_true_unsupported"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), \
                       BASECONFIG + \
                       "[storage]\n" + \
                       "enabled = true\n" + \
                       "debug_discard = true\n")
        self.failUnlessRaises(OldConfigOptionError, client.Client, basedir)

    def test_web_staticdir(self):
        basedir = u"client.Basic.test_web_staticdir"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       BASECONFIG +
                       "[node]\n" +
                       "web.port = tcp:0:interface=127.0.0.1\n" +
                       "web.static = relative\n")
        c = client.Client(basedir)
        w = c.getServiceNamed("webish")
        abs_basedir = fileutil.abspath_expanduser_unicode(basedir)
        expected = fileutil.abspath_expanduser_unicode(u"relative", abs_basedir)
        self.failUnlessReallyEqual(w.staticdir, expected)

    # TODO: also test config options for SFTP.

    def test_ftp_auth_keyfile(self):
        basedir = u"client.Basic.test_ftp_auth_keyfile"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       (BASECONFIG +
                        "[ftpd]\n"
                        "enabled = true\n"
                        "port = tcp:0:interface=127.0.0.1\n"
                        "accounts.file = private/accounts\n"))
        os.mkdir(os.path.join(basedir, "private"))
        fileutil.write(os.path.join(basedir, "private", "accounts"), "\n")
        c = client.Client(basedir) # just make sure it can be instantiated
        del c

    def test_ftp_auth_url(self):
        basedir = u"client.Basic.test_ftp_auth_url"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       (BASECONFIG +
                        "[ftpd]\n"
                        "enabled = true\n"
                        "port = tcp:0:interface=127.0.0.1\n"
                        "accounts.url = http://0.0.0.0/\n"))
        c = client.Client(basedir) # just make sure it can be instantiated
        del c

    def test_ftp_auth_no_accountfile_or_url(self):
        basedir = u"client.Basic.test_ftp_auth_no_accountfile_or_url"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                       (BASECONFIG +
                        "[ftpd]\n"
                        "enabled = true\n"
                        "port = tcp:0:interface=127.0.0.1\n"))
        self.failUnlessRaises(NeedRootcapLookupScheme, client.Client, basedir)

    def _permute(self, sb, key):
        return [ base32.a2b(s.get_longname()) for s in sb.get_servers_for_psi(key) ]

    def test_permute(self):
        sb = StorageFarmBroker(True, None)
        for k in ["%d" % i for i in range(5)]:
            ann = {"anonymous-storage-FURL": "pb://%s@nowhere/fake" % base32.b2a(k),
                   "permutation-seed-base32": base32.b2a(k) }
            sb.test_add_rref(k, "rref", ann)

        self.failUnlessReallyEqual(self._permute(sb, "one"), ['3','1','0','4','2'])
        self.failUnlessReallyEqual(self._permute(sb, "two"), ['0','4','2','1','3'])
        sb.servers.clear()
        self.failUnlessReallyEqual(self._permute(sb, "one"), [])

    def test_permute_with_preferred(self):
        sb = StorageFarmBroker(True, None, preferred_peers=['1','4'])
        for k in ["%d" % i for i in range(5)]:
            ann = {"anonymous-storage-FURL": "pb://abcde@nowhere/fake",
                   "permutation-seed-base32": base32.b2a(k) }
            sb.test_add_rref(k, "rref", ann)

        self.failUnlessReallyEqual(self._permute(sb, "one"), ['1','4','3','0','2'])
        self.failUnlessReallyEqual(self._permute(sb, "two"), ['4','1','0','2','3'])
        sb.servers.clear()
        self.failUnlessReallyEqual(self._permute(sb, "one"), [])

    def test_versions(self):
        basedir = "test_client.Basic.test_versions"
        os.mkdir(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), \
                           BASECONFIG + \
                           "[storage]\n" + \
                           "enabled = true\n")
        c = client.Client(basedir)
        server = c.getServiceNamed("storage")
        aa = server.get_accountant().get_anonymous_account()
        verdict = aa.remote_get_version()
        self.failUnlessReallyEqual(verdict["application-version"],
                                   str(allmydata.__full_version__))
        self.failIfEqual(str(allmydata.__version__), "unknown")
        self.failUnless("." in str(allmydata.__full_version__),
                        "non-numeric version in '%s'" % allmydata.__version__)
        all_versions = allmydata.get_package_versions_string()
        self.failUnless(allmydata.__appname__ in all_versions)
        # also test stats
        stats = c.get_stats()
        self.failUnless("node.uptime" in stats)
        self.failUnless(isinstance(stats["node.uptime"], float))

    def test_helper_furl(self):
        basedir = "test_client.Basic.test_helper_furl"
        os.mkdir(basedir)

        def _check(config, expected_furl):
            fileutil.write(os.path.join(basedir, "tahoe.cfg"),
                           BASECONFIG + config)
            c = client.Client(basedir)
            uploader = c.getServiceNamed("uploader")
            furl, connected = uploader.get_helper_info()
            self.failUnlessEqual(furl, expected_furl)

        _check("", None)
        _check("helper.furl =\n", None)
        _check("helper.furl = \n", None)
        _check("helper.furl = None", None)
        _check("helper.furl = pb://blah\n", "pb://blah")

    def test_create_magic_folder_service(self):
        class MockMagicFolder(service.MultiService):
            name = 'magic-folder'

            def __init__(self, client, upload_dircap, collective_dircap, local_path_u, dbfile, umask, inotify=None,
                         uploader_delay=1.0, clock=None, downloader_delay=3):
                service.MultiService.__init__(self)
                self.client = client
                self._umask = umask
                self.upload_dircap = upload_dircap
                self.collective_dircap = collective_dircap
                self.local_dir = local_path_u
                self.dbfile = dbfile
                self.inotify = inotify

            def ready(self):
                pass

        self.patch(allmydata.frontends.magic_folder, 'MagicFolder', MockMagicFolder)

        upload_dircap = "URI:DIR2:blah"
        local_dir_u = self.unicode_or_fallback(u"loc\u0101l_dir", u"local_dir")
        local_dir_utf8 = local_dir_u.encode('utf-8')
        config = (BASECONFIG +
                  "[storage]\n" +
                  "enabled = false\n" +
                  "[magic_folder]\n" +
                  "enabled = true\n")

        basedir1 = "test_client.Basic.test_create_magic_folder_service1"
        os.mkdir(basedir1)

        fileutil.write(os.path.join(basedir1, "tahoe.cfg"),
                       config + "local.directory = " + local_dir_utf8 + "\n")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir1)

        fileutil.write(os.path.join(basedir1, "tahoe.cfg"), config)
        fileutil.write(os.path.join(basedir1, "private", "magic_folder_dircap"), "URI:DIR2:blah")
        fileutil.write(os.path.join(basedir1, "private", "collective_dircap"), "URI:DIR2:meow")
        self.failUnlessRaises(MissingConfigEntry, client.Client, basedir1)

        fileutil.write(os.path.join(basedir1, "tahoe.cfg"),
                       config.replace("[magic_folder]\n", "[drop_upload]\n"))
        self.failUnlessRaises(OldConfigOptionError, client.Client, basedir1)

        fileutil.write(os.path.join(basedir1, "tahoe.cfg"),
                       config + "local.directory = " + local_dir_utf8 + "\n")
        c1 = client.Client(basedir1)
        magicfolder = c1.getServiceNamed('magic-folder')
        self.failUnless(isinstance(magicfolder, MockMagicFolder), magicfolder)
        self.failUnlessReallyEqual(magicfolder.client, c1)
        self.failUnlessReallyEqual(magicfolder.upload_dircap, upload_dircap)
        self.failUnlessReallyEqual(os.path.basename(magicfolder.local_dir), local_dir_u)
        self.failUnless(magicfolder.inotify is None, magicfolder.inotify)
        self.failUnless(magicfolder.running)

        class Boom(Exception):
            pass
        def BoomMagicFolder(client, upload_dircap, collective_dircap, local_path_u, dbfile,
                            umask, inotify=None, uploader_delay=1.0, clock=None, downloader_delay=3):
            raise Boom()
        self.patch(allmydata.frontends.magic_folder, 'MagicFolder', BoomMagicFolder)

        basedir2 = "test_client.Basic.test_create_magic_folder_service2"
        os.mkdir(basedir2)
        os.mkdir(os.path.join(basedir2, "private"))
        fileutil.write(os.path.join(basedir2, "tahoe.cfg"),
                       BASECONFIG +
                       "[magic_folder]\n" +
                       "enabled = true\n" +
                       "local.directory = " + local_dir_utf8 + "\n")
        fileutil.write(os.path.join(basedir2, "private", "magic_folder_dircap"), "URI:DIR2:blah")
        fileutil.write(os.path.join(basedir2, "private", "collective_dircap"), "URI:DIR2:meow")
        self.failUnlessRaises(Boom, client.Client, basedir2)


def flush_but_dont_ignore(res):
    d = flushEventualQueue()
    def _done(ignored):
        return res
    d.addCallback(_done)
    return d

class Run(unittest.TestCase, testutil.StallMixin):

    def setUp(self):
        self.sparent = service.MultiService()
        self.sparent.startService()
    def tearDown(self):
        d = self.sparent.stopService()
        d.addBoth(flush_but_dont_ignore)
        return d

    def test_loadable(self):
        basedir = "test_client.Run.test_loadable"
        os.mkdir(basedir)
        dummy = "pb://wl74cyahejagspqgy4x5ukrvfnevlknt@127.0.0.1:58889/bogus"
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), BASECONFIG_I % dummy)
        fileutil.write(os.path.join(basedir, client.Client.EXIT_TRIGGER_FILE), "")
        client.Client(basedir)

    def test_reloadable(self):
        basedir = "test_client.Run.test_reloadable"
        os.mkdir(basedir)
        dummy = "pb://wl74cyahejagspqgy4x5ukrvfnevlknt@127.0.0.1:58889/bogus"
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), BASECONFIG_I % dummy)
        c1 = client.Client(basedir)
        c1.setServiceParent(self.sparent)

        # delay to let the service start up completely. I'm not entirely sure
        # this is necessary.
        d = self.stall(delay=2.0)
        d.addCallback(lambda res: c1.disownServiceParent())
        # the cygwin buildslave seems to need more time to let the old
        # service completely shut down. When delay=0.1, I saw this test fail,
        # probably due to the logport trying to reclaim the old socket
        # number. This suggests that either we're dropping a Deferred
        # somewhere in the shutdown sequence, or that cygwin is just cranky.
        d.addCallback(self.stall, delay=2.0)
        def _restart(res):
            # TODO: pause for slightly over one second, to let
            # Client._check_exit_trigger poll the file once. That will exercise
            # another few lines. Then add another test in which we don't
            # update the file at all, and watch to see the node shutdown.
            # (To do this, use a modified node which overrides Node.shutdown(),
            # also change _check_exit_trigger to use it instead of a raw
            # reactor.stop, also instrument the shutdown event in an
            # attribute that we can check.)
            c2 = client.Client(basedir)
            c2.setServiceParent(self.sparent)
            return c2.disownServiceParent()
        d.addCallback(_restart)
        return d

class NodeMaker(testutil.ReallyEqualMixin, unittest.TestCase):
    def test_maker(self):
        basedir = "client/NodeMaker/maker"
        fileutil.make_dirs(basedir)
        fileutil.write(os.path.join(basedir, "tahoe.cfg"), BASECONFIG)
        c = client.Client(basedir)

        n = c.create_node_from_uri("URI:CHK:6nmrpsubgbe57udnexlkiwzmlu:bjt7j6hshrlmadjyr7otq3dc24end5meo5xcr5xe5r663po6itmq:3:10:7277")
        self.failUnless(IFilesystemNode.providedBy(n))
        self.failUnless(IFileNode.providedBy(n))
        self.failUnless(IImmutableFileNode.providedBy(n))
        self.failIf(IMutableFileNode.providedBy(n))
        self.failIf(IDirectoryNode.providedBy(n))
        self.failUnless(n.is_readonly())
        self.failIf(n.is_mutable())

        # Testing #1679. There was a bug that would occur when downloader was
        # downloading the same readcap more than once concurrently, so the
        # filenode object was cached, and there was a failure from one of the
        # servers in one of the download attempts. No subsequent download
        # attempt would attempt to use that server again, which would lead to
        # the file being undownloadable until the gateway was restarted. The
        # current fix for this (hopefully to be superceded by a better fix
        # eventually) is to prevent re-use of filenodes, so the NodeMaker is
        # hereby required *not* to cache and re-use filenodes for CHKs.
        other_n = c.create_node_from_uri("URI:CHK:6nmrpsubgbe57udnexlkiwzmlu:bjt7j6hshrlmadjyr7otq3dc24end5meo5xcr5xe5r663po6itmq:3:10:7277")
        self.failIf(n is other_n, (n, other_n))

        n = c.create_node_from_uri("URI:LIT:n5xgk")
        self.failUnless(IFilesystemNode.providedBy(n))
        self.failUnless(IFileNode.providedBy(n))
        self.failUnless(IImmutableFileNode.providedBy(n))
        self.failIf(IMutableFileNode.providedBy(n))
        self.failIf(IDirectoryNode.providedBy(n))
        self.failUnless(n.is_readonly())
        self.failIf(n.is_mutable())

        n = c.create_node_from_uri("URI:SSK:n6x24zd3seu725yluj75q5boaa:mm6yoqjhl6ueh7iereldqxue4nene4wl7rqfjfybqrehdqmqskvq")
        self.failUnless(IFilesystemNode.providedBy(n))
        self.failUnless(IFileNode.providedBy(n))
        self.failIf(IImmutableFileNode.providedBy(n))
        self.failUnless(IMutableFileNode.providedBy(n))
        self.failIf(IDirectoryNode.providedBy(n))
        self.failIf(n.is_readonly())
        self.failUnless(n.is_mutable())

        n = c.create_node_from_uri("URI:SSK-RO:b7sr5qsifnicca7cbk3rhrhbvq:mm6yoqjhl6ueh7iereldqxue4nene4wl7rqfjfybqrehdqmqskvq")
        self.failUnless(IFilesystemNode.providedBy(n))
        self.failUnless(IFileNode.providedBy(n))
        self.failIf(IImmutableFileNode.providedBy(n))
        self.failUnless(IMutableFileNode.providedBy(n))
        self.failIf(IDirectoryNode.providedBy(n))
        self.failUnless(n.is_readonly())
        self.failUnless(n.is_mutable())

        n = c.create_node_from_uri("URI:DIR2:n6x24zd3seu725yluj75q5boaa:mm6yoqjhl6ueh7iereldqxue4nene4wl7rqfjfybqrehdqmqskvq")
        self.failUnless(IFilesystemNode.providedBy(n))
        self.failIf(IFileNode.providedBy(n))
        self.failIf(IImmutableFileNode.providedBy(n))
        self.failIf(IMutableFileNode.providedBy(n))
        self.failUnless(IDirectoryNode.providedBy(n))
        self.failIf(n.is_readonly())
        self.failUnless(n.is_mutable())

        n = c.create_node_from_uri("URI:DIR2-RO:b7sr5qsifnicca7cbk3rhrhbvq:mm6yoqjhl6ueh7iereldqxue4nene4wl7rqfjfybqrehdqmqskvq")
        self.failUnless(IFilesystemNode.providedBy(n))
        self.failIf(IFileNode.providedBy(n))
        self.failIf(IImmutableFileNode.providedBy(n))
        self.failIf(IMutableFileNode.providedBy(n))
        self.failUnless(IDirectoryNode.providedBy(n))
        self.failUnless(n.is_readonly())
        self.failUnless(n.is_mutable())

        unknown_rw = "lafs://from_the_future"
        unknown_ro = "lafs://readonly_from_the_future"
        n = c.create_node_from_uri(unknown_rw, unknown_ro)
        self.failUnless(IFilesystemNode.providedBy(n))
        self.failIf(IFileNode.providedBy(n))
        self.failIf(IImmutableFileNode.providedBy(n))
        self.failIf(IMutableFileNode.providedBy(n))
        self.failIf(IDirectoryNode.providedBy(n))
        self.failUnless(n.is_unknown())
        self.failUnlessReallyEqual(n.get_uri(), unknown_rw)
        self.failUnlessReallyEqual(n.get_write_uri(), unknown_rw)
        self.failUnlessReallyEqual(n.get_readonly_uri(), "ro." + unknown_ro)

        # Note: it isn't that we *intend* to deploy non-ASCII caps in
        # the future, it is that we want to make sure older Tahoe-LAFS
        # versions wouldn't choke on them if we were to do so. See
        # #1051 and wiki:NewCapDesign for details.
        unknown_rw = u"lafs://from_the_future_rw_\u263A".encode('utf-8')
        unknown_ro = u"lafs://readonly_from_the_future_ro_\u263A".encode('utf-8')
        n = c.create_node_from_uri(unknown_rw, unknown_ro)
        self.failUnless(IFilesystemNode.providedBy(n))
        self.failIf(IFileNode.providedBy(n))
        self.failIf(IImmutableFileNode.providedBy(n))
        self.failIf(IMutableFileNode.providedBy(n))
        self.failIf(IDirectoryNode.providedBy(n))
        self.failUnless(n.is_unknown())
        self.failUnlessReallyEqual(n.get_uri(), unknown_rw)
        self.failUnlessReallyEqual(n.get_write_uri(), unknown_rw)
        self.failUnlessReallyEqual(n.get_readonly_uri(), "ro." + unknown_ro)
