
import os, sys

from twisted.trial import unittest
from twisted.python import filepath, runtime
from twisted.internet import defer

from allmydata.interfaces import IDirectoryNode, NoSuchChildError

from allmydata.util import fileutil, fake_inotify
from allmydata.util.encodingutil import get_filesystem_encoding
from allmydata.util.consumer import download_to_data
from allmydata.test.no_network import GridTestMixin
from allmydata.test.common_util import ReallyEqualMixin, NonASCIIPathMixin
from allmydata.test.common import ShouldFailMixin

from allmydata.frontends.drop_upload import DropUploader
from allmydata.scripts import backupdb


class DropUploadTestMixin(GridTestMixin, ShouldFailMixin, ReallyEqualMixin, NonASCIIPathMixin):
    """
    These tests will be run both with a mock notifier, and (on platforms that support it)
    with the real INotify.
    """

    def _get_count(self, name):
        return self.stats_provider.get_stats()["counters"].get(name, 0)


    def create(self, dbfile):
        bdb = backupdb.get_backupdb(dbfile)
        self.failUnless(bdb, "unable to create backupdb from %r" % (dbfile,))
        return bdb

    def _test_db_basic(self):
        fileutil.make_dirs(self.basedir)
        dbfile = os.path.join(self.basedir, "dbfile")
        bdb = self.create(dbfile)

    def _test_uploader_start_service(self):
        self.uploader = None
        self.set_up_grid()
        self.client = self.g.clients[0]

        d = self.client.create_dirnode()
        def _made_upload_dir(n):
            self.failUnless(IDirectoryNode.providedBy(n))
            self.upload_dirnode = n
            self.upload_dircap = n.get_uri()
            self.uploader = DropUploader(self.client, self.upload_dircap, self.basedir.encode('utf-8'),
                                         "magicfolderdb.sqlite", inotify=self.inotify)
            self.uploader.startService()
            self.failUnlessEqual(self.uploader._db.VERSION, 2)
        d.addCallback(_made_upload_dir)

        # Prevent unclean reactor errors.
        def _cleanup(res):
            d = defer.succeed(None)
            if self.uploader is not None:
                d.addCallback(lambda ign: self.uploader.finish(for_tests=True))
            d.addCallback(lambda ign: res)
            return d
        d.addBoth(_cleanup)
        return d

    def _test(self):
        self.uploader = None
        self.set_up_grid()
        self.local_dir = os.path.join(self.basedir, self.unicode_or_fallback(u"loc\u0101l_dir", u"local_dir"))
        self.mkdir_nonascii(self.local_dir)

        self.client = self.g.clients[0]
        self.stats_provider = self.client.stats_provider

        d = self.client.create_dirnode()
        def _made_upload_dir(n):
            self.failUnless(IDirectoryNode.providedBy(n))
            self.upload_dirnode = n
            self.upload_dircap = n.get_uri()
            self.uploader = DropUploader(self.client, self.upload_dircap, self.local_dir.encode('utf-8'),
                                         "magicfolderdb.sqlite", inotify=self.inotify, pending_delay=0.2)
            self.uploader.setServiceParent(self.client)
            d = self.uploader.startService()
            self.uploader.upload_ready()
            return d

        d.addCallback(_made_upload_dir)

        # Write something short enough for a LIT file.
        d.addCallback(lambda ign: self._test_file(u"short", "test"))

        # XXX FIX ME
        # Write to the same file again with different data.
        #d.addCallback(lambda ign: self._test_file(u"short", "different"))

        # Test that temporary files are not uploaded.
        d.addCallback(lambda ign: self._test_file(u"tempfile", "test", temporary=True))

        # Test that we tolerate creation of a subdirectory.
        #d.addCallback(lambda ign: os.mkdir(os.path.join(self.local_dir, u"directory")))

        # Write something longer, and also try to test a Unicode name if the fs can represent it.
        name_u = self.unicode_or_fallback(u"l\u00F8ng", u"long")
        d.addCallback(lambda ign: self._test_file(name_u, "test"*100))

        # TODO: test that causes an upload failure.
        d.addCallback(lambda ign: self.failUnlessReallyEqual(self._get_count('drop_upload.files_failed'), 0))

        # Prevent unclean reactor errors.
        def _cleanup(res):
            d = defer.succeed(None)
            if self.uploader is not None:
                d.addCallback(lambda ign: self.uploader.finish(for_tests=True))
            d.addCallback(lambda ign: res)
            return d
        d.addBoth(_cleanup)
        return d

    def _test_file(self, name_u, data, temporary=False):
        previously_uploaded = self._get_count('drop_upload.objects_uploaded')
        previously_disappeared = self._get_count('drop_upload.objects_disappeared')

        d = defer.Deferred()

        # Note: this relies on the fact that we only get one IN_CLOSE_WRITE notification per file
        # (otherwise we would get a defer.AlreadyCalledError). Should we be relying on that?
        self.uploader.set_uploaded_callback(d.callback)

        path_u = os.path.join(self.local_dir, name_u)
        if sys.platform == "win32":
            path = filepath.FilePath(path_u)
        else:
            path = filepath.FilePath(path_u.encode(get_filesystem_encoding()))

        # We don't use FilePath.setContent() here because it creates a temporary file that
        # is renamed into place, which causes events that the test is not expecting.
        f = open(path.path, "wb")
        try:
            if temporary and sys.platform != "win32":
                os.unlink(path.path)
            f.write(data)
        finally:
            f.close()
        if temporary and sys.platform == "win32":
            os.unlink(path.path)
        fileutil.flush_volume(path.path)
        self.notify_close_write(path)

        if temporary:
            d.addCallback(lambda ign: self.shouldFail(NoSuchChildError, 'temp file not uploaded', None,
                                                      self.upload_dirnode.get, name_u))
            d.addCallback(lambda ign: self.failUnlessReallyEqual(self._get_count('drop_upload.objects_disappeared'),
                                                                 previously_disappeared + 1))
        else:
            d.addCallback(lambda ign: self.upload_dirnode.get(name_u))
            d.addCallback(download_to_data)
            d.addCallback(lambda actual_data: self.failUnlessReallyEqual(actual_data, data))
            d.addCallback(lambda ign: self.failUnlessReallyEqual(self._get_count('drop_upload.objects_uploaded'),
                                                                 previously_uploaded + 1))

        d.addCallback(lambda ign: self.failUnlessReallyEqual(self._get_count('drop_upload.objects_queued'), 0))
        return d


class MockTest(DropUploadTestMixin, unittest.TestCase):
    """This can run on any platform, and even if twisted.internet.inotify can't be imported."""

    def test_errors(self):
        self.basedir = "drop_upload.MockTest.test_errors"
        self.set_up_grid()
        errors_dir = os.path.join(self.basedir, "errors_dir")
        os.mkdir(errors_dir)
        not_a_dir = os.path.join(self.basedir, 'NOT_A_DIR')
        fileutil.write(not_a_dir, "")

        client = self.g.clients[0]
        d = client.create_dirnode()
        def _made_upload_dir(n):
            self.failUnless(IDirectoryNode.providedBy(n))
            upload_dircap = n.get_uri()
            readonly_dircap = n.get_readonly_uri()

            self.shouldFail(AssertionError, 'invalid local.directory', 'could not be represented',
                            DropUploader, client, upload_dircap, '\xFF', 'magicfolderdb', inotify=fake_inotify)
            self.shouldFail(AssertionError, 'nonexistent local.directory', 'there is no directory',
                            DropUploader, client, upload_dircap, os.path.join(self.basedir, "Laputa"), 'magicfolderdb', inotify=fake_inotify)
            self.shouldFail(AssertionError, 'non-directory local.directory', 'is not a directory',
                            DropUploader, client, upload_dircap, not_a_dir, 'magicfolderdb', inotify=fake_inotify)
            self.shouldFail(AssertionError, 'bad upload.dircap', 'does not refer to a directory',
                            DropUploader, client, 'bad', errors_dir, 'magicfolderdb', inotify=fake_inotify)
            self.shouldFail(AssertionError, 'non-directory upload.dircap', 'does not refer to a directory',
                            DropUploader, client, 'URI:LIT:foo', errors_dir, 'magicfolderdb', inotify=fake_inotify)
            self.shouldFail(AssertionError, 'readonly upload.dircap', 'is not a writecap to a directory',
                            DropUploader, client, readonly_dircap, errors_dir, 'magicfolderdb', inotify=fake_inotify)
        d.addCallback(_made_upload_dir)
        return d

    def test_drop_upload(self):
        self.inotify = fake_inotify
        self.basedir = "drop_upload.MockTest.test_drop_upload"
        return self._test()

    def test_basic_db(self):
        self.inotify = fake_inotify
        self.basedir = "drop_upload.MockTest.test_basic_db"
        return self._test_db_basic()

    def test_uploader_start_service(self):
        self.inotify = fake_inotify
        self.basedir = "drop_upload.MockTest._test_uploader_start_service"
        return self._test_uploader_start_service()

    def notify_close_write(self, path):
        self.uploader._notifier.event(path, self.inotify.IN_CLOSE_WRITE)


class RealTest(DropUploadTestMixin, unittest.TestCase):
    """This is skipped unless both Twisted and the platform support inotify."""


    def test_drop_upload(self):
        # We should always have runtime.platform.supportsINotify, because we're using
        # Twisted >= 10.1.
        if sys.platform != "win32" and not runtime.platform.supportsINotify():
            raise unittest.SkipTest("Drop-upload support can only be tested for-real on an OS that supports inotify or equivalent.")

        self.inotify = None  # use the appropriate inotify for the platform
        self.basedir = "drop_upload.RealTest.test_drop_upload"
        return self._test()

    def notify_close_write(self, path):
        # Writing to the file causes the notification.
        pass

    def test_basic_db(self):
        self.inotify = None
        self.basedir = "drop_upload.RealTest.test_basic_db"
        return self._test_db_basic()

    def test_uploader_start_service(self):
        self.inotify = None
        self.basedir = "drop_upload.RealTest._test_uploader_start_service"
        return self._test_uploader_start_service()
