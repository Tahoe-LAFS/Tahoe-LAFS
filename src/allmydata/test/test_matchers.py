"""
Tests for ``allmydata.test.matchers``.
"""

from fixtures import (
    TempDir,
)

from testtools.matchers import (
    Not,
    Is,
)

from hypothesis import (
    given,
)

from twisted.internet.defer import (
    inlineCallbacks,
)

from ..crypto import (
    ed25519,
)
from ..client import (
    create_client,
)
from .common import (
    SyncTestCase,
    Announcement,
)

from .strategies import (
    storage_announcements,
)

from .matchers import (
    MatchesNodePublicKey,
    matches_storage_announcement,
)


class MatchesNodePublicKeyTestCase(SyncTestCase):
    """
    Tests for ``MatchesNodePublicKey``.
    """
    @inlineCallbacks
    def setUp(self):
        super(MatchesNodePublicKeyTestCase, self).setUp()
        self.tempdir = self.useFixture(TempDir())
        self.basedir = self.tempdir.join(b"node")
        yield create_client(self.basedir)

    def test_match(self):
        """
        ``MatchesNodePublicKey.match`` returns ``None`` when called with the same
        private key as is used by the node at the directory it is configured
        with.
        """
        with open(self.tempdir.join(b"node", b"private", b"node.privkey")) as key_file:
            key_bytes = key_file.read()
        private_key = ed25519.signing_keypair_from_string(key_bytes.strip())[0]
        matcher = MatchesNodePublicKey(self.basedir)
        self.assertThat(
            matcher.match(private_key),
            Is(None),
        )

    def test_mismatch(self):
        """
        ``MatchesNodePublicKey.match`` returns other than ``None`` when called
        with a different private key than is used by the node at the directory
        it is configured with.
        """
        other_private_key = ed25519.create_signing_keypair()[0]
        matcher = MatchesNodePublicKey(self.basedir)
        self.assertThat(
            matcher.match(other_private_key),
            Not(Is(None)),
        )


class MatchesStorageAnnouncementTests(SyncTestCase):
    """
    Tests for ``matches_storage_announcement``.
    """
    @inlineCallbacks
    def setUp(self):
        super(MatchesStorageAnnouncementTests, self).setUp()
        self.tempdir = self.useFixture(TempDir())
        self.basedir = self.tempdir.join(b"node")
        yield create_client(self.basedir)

    def test_mismatch_missing_permutation_seed(self, announcement):
        """
        An announcement without a **permutation-seed-base32** item does not match.
        """
        del announcement[u"permutation-seed-base32"]
        matcher = matches_storage_announcement(self.basedir)
        self.assertThat(
            matcher.match(Announcement(**announcement)),
            Not(Is(None)),
        )
