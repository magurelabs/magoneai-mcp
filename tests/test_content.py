"""The two properties a content handle exists to guarantee.

A handle replaces giving every connector the object-store keys. That is only
an improvement if two things hold, so both are asserted directly rather than
through a tool that happens to use them.

**The key is ours.** A caller that names its own key can name a key another
tenant is about to write. It never has to overwrite anything: the victim's
write fails and the victim's reader gets the attacker's bytes. An owner tag on
an existing object does not stop that, because squatting is not overwriting.

**A handle is single-use and expires.** Otherwise it is a bearer credential
with no revocation.
"""

import time

import pytest

from magone_mcp_core.content import (
    ContentHandleError,
    Direction,
    HandleExpired,
    HandleRedeemed,
    HandleState,
    PresignedMinter,
    safe_display_name,
)


class FakeSigner:
    """Deterministic, and records what it was asked to sign."""

    def __init__(self):
        self.signed: list[tuple[str, Direction, int]] = []

    def sign(self, object_key: str, direction: Direction, ttl_seconds: int) -> str:
        self.signed.append((object_key, direction, ttl_seconds))
        return f"https://store.invalid/{object_key}?d={direction.value}&ttl={ttl_seconds}"


def minter(**kwargs) -> PresignedMinter:
    return PresignedMinter(FakeSigner(), **kwargs)


class TestTheKeyIsOurs:
    async def test_a_caller_cannot_choose_the_key(self):
        """The whole point. Two uploads of the same name are different objects."""
        service = minter()

        first = await service.issue_upload("project-a", "invoice.pdf")
        second = await service.issue_upload("project-a", "invoice.pdf")

        assert first.object_key != second.object_key

    async def test_a_caller_cannot_squat_another_principals_key(self):
        """Same filename, different principal, and the keys still cannot collide."""
        service = minter()

        mine = await service.issue_upload("project-a", "report.csv")
        theirs = await service.issue_upload("project-b", "report.csv")

        assert mine.object_key != theirs.object_key
        assert "project-b" not in mine.object_key

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "/absolute/path.txt",
            "..",
            "a/b/c.txt",
            "\\windows\\system32\\x.dll",
            "réport.csv",
            "\x00null.txt",
        ],
    )
    async def test_a_hostile_filename_cannot_shape_the_key(self, hostile):
        service = minter()

        handle = await service.issue_upload("p", hostile)

        segments = handle.object_key.split("/")
        assert ".." not in segments
        assert "" not in segments
        assert handle.object_key.isascii()
        assert handle.object_key.startswith("handles/p/")

    async def test_an_unnameable_filename_still_yields_a_usable_key(self):
        """`...` reduces to nothing; an empty final segment is a different key."""
        service = minter()

        handle = await service.issue_upload("p", "...")

        assert handle.object_key.split("/")[-1].endswith("_file")

    async def test_download_refuses_a_traversing_key(self):
        service = minter()

        with pytest.raises(ContentHandleError):
            await service.issue_download("p", "handles/p/../../secrets")


class TestSingleUseAndExpiry:
    async def test_a_handle_is_redeemable_once(self):
        service = minter()
        handle = await service.issue_upload("p", "x.csv")

        await service.redeem(handle.id, handle.principal)

        with pytest.raises(HandleRedeemed):
            await service.redeem(handle.id, handle.principal)

    async def test_redemption_records_when_and_moves_the_state(self):
        service = minter()
        handle = await service.issue_upload("p", "x.csv")
        assert handle.state is HandleState.MINTED and handle.redeemed_at is None

        redeemed = await service.redeem(handle.id, handle.principal)

        assert redeemed.state is HandleState.REDEEMED
        assert redeemed.redeemed_at is not None

    async def test_an_expired_handle_is_refused_rather_than_returned(self):
        service = minter()
        handle = await service.issue_upload("p", "x.csv", ttl_seconds=1)

        # Reach past the clock rather than sleeping; the assertion is about the
        # check, not about timing.
        service._store._handles[handle.id] = type(handle)(
            **{**handle.__dict__, "expires_at": time.time() - 1}
        )

        with pytest.raises(HandleExpired):
            await service.redeem(handle.id, handle.principal)

    async def test_a_zero_lifetime_handle_is_never_issued(self):
        service = minter()

        with pytest.raises(ContentHandleError):
            await service.issue_upload("p", "x.csv", ttl_seconds=0)

    async def test_an_unknown_handle_does_not_resolve(self):
        service = minter()

        with pytest.raises(ContentHandleError):
            await service.redeem("does-not-exist", "p")


class TestWhatTheCallerGets:
    async def test_the_signer_is_asked_for_the_key_the_handle_reports(self):
        """A handle whose URL points somewhere other than its recorded key
        would make the audit record fiction."""
        signer = FakeSigner()
        service = PresignedMinter(signer)

        handle = await service.issue_upload("p", "x.csv", ttl_seconds=60)

        assert signer.signed == [(handle.object_key, Direction.UPLOAD, 60)]
        assert handle.object_key in handle.url

    async def test_direction_is_carried_to_the_signer(self):
        signer = FakeSigner()
        service = PresignedMinter(signer)

        await service.issue_download("p", "handles/p/abc_x.csv", ttl_seconds=30)

        assert signer.signed[0][1] is Direction.DOWNLOAD

    async def test_a_principal_is_required(self):
        service = minter()

        with pytest.raises(ContentHandleError):
            await service.issue_upload("", "x.csv")

    async def test_the_display_name_survives_for_the_user(self):
        """The key is infrastructure; the user still needs to see a filename."""
        service = minter()

        handle = await service.issue_upload("p", "Q3 report.csv")

        assert handle.display_name == "Q3_report.csv"


class TestSafeDisplayName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain.txt", "plain.txt"),
            ("a/b/c.txt", "c.txt"),
            ("../../x.txt", "x.txt"),
            ("  spaced .txt", "spaced.txt"),
            ("", "file"),
            ("...", "file"),
            ("تقرير.pptx", "file.pptx"),
            ("тест-файл.csv", "file.csv"),
        ],
    )
    def test_reduces_to_something_usable(self, raw, expected):
        assert safe_display_name(raw) == expected

    def test_it_is_always_ascii(self):
        """Because the name comes back out through an HTTP header, and a
        non-ASCII byte in one raises rather than returns."""
        assert safe_display_name("тест-файл.csv").isascii()

    def test_it_is_bounded(self):
        assert len(safe_display_name("a" * 500)) <= 120


class TestAHandleIsBoundToItsPrincipal:
    """A handle is a name, not a capability."""

    async def test_a_download_of_another_principals_object_is_refused(self):
        service = PresignedMinter(FakeSigner())

        with pytest.raises(ContentHandleError):
            await service.issue_download("tenant-b", "handles/tenant-a/abc_report.csv")

    async def test_a_download_of_the_principals_own_object_is_allowed(self):
        service = PresignedMinter(FakeSigner())
        upload = await service.issue_upload("tenant-a", "report.csv")

        download = await service.issue_download("tenant-a", upload.object_key)

        assert download.object_key == upload.object_key

    @pytest.mark.parametrize("key", ["/etc/passwd", "handles/tenant-a/../tenant-b/x"])
    async def test_an_absolute_or_escaping_key_is_refused(self, key):
        service = PresignedMinter(FakeSigner())

        with pytest.raises(ContentHandleError):
            await service.issue_download("tenant-a", key)

    async def test_a_download_with_no_lifetime_is_refused(self):
        service = PresignedMinter(FakeSigner())

        with pytest.raises(ContentHandleError):
            await service.issue_download("p", "handles/p/abc_x.csv", ttl_seconds=0)

    async def test_another_principal_cannot_redeem_a_handle(self):
        service = PresignedMinter(FakeSigner())
        handle = await service.issue_upload("tenant-a", "x.csv")

        with pytest.raises(ContentHandleError):
            await service.redeem(handle.id, "tenant-b")
        # and the owner still can: the refused attempt consumed nothing
        assert (await service.redeem(handle.id, "tenant-a")).redeemed_at is not None

    async def test_lifetimes_are_bounded(self):
        import time

        service = PresignedMinter(FakeSigner())
        upload = await service.issue_upload("p", "x.csv")
        download = await service.issue_download("p", upload.object_key)

        assert 14 * 60 < upload.expires_at - time.time() <= 15 * 60
        assert 59 * 60 < download.expires_at - time.time() <= 60 * 60
