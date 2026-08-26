"""
Findings from a line-by-line comparison against the two reference projects.

The owner's instruction was blunt and fair: read the reference code, we hit these
problems there already. The audit compared every public function in the client and
infrastructure layers against both references. Most of the 286 differences are
deliberate — linkdooni, the automation menu, payments/TRX, admin management were
all removed on purpose, and many names changed to become customer-scoped. Four
were real:

  1. The Dockerfile lacked `-i https://pypi.org/simple`, which BOTH references
     carry. A server with a pip.conf pointing at a mirror that serves sdists makes
     pip COMPILE telethon from source — which is what filled a disk.
  2. Contact export read a function with no phone field, so it always returned
     nothing, silently.
  3. GIT_BRANCH defaulted to "main", which holds only a README.
  4. Nothing handled a full disk or a transient Docker Hub 502 — neither reference
     did either, so these are genuinely new.
"""
import os

import pytest

import config
import rubika_client as rb
import worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _code(name):
    """Source with comments stripped.

    These checks name the very strings they guard, in comments beside the fix, so
    a raw text search finds the explanation and passes while the code is broken.
    Mutation testing caught exactly that: deleting the real `-i` flag left the one
    in the comment and the test stayed green.
    """
    out = []
    for line in _src(name).splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


class _User:
    """A Rubika contact as the API actually returns it."""

    def __init__(self, guid, phone):
        self.user_guid = guid
        self.phone = phone
        self.first_name = "N"
        self.last_name = ""


class _Page:
    def __init__(self, users):
        self.users = users


class _Client:
    def __init__(self, pages):
        self._pages = pages
        self.calls = 0

    async def get_contacts(self, start_id=None):
        page = self._pages[min(self.calls, len(self._pages) - 1)]
        self.calls += 1
        return page


# --------------------------------------------------------------------------- #
# 1. Contact export actually returns phone numbers
# --------------------------------------------------------------------------- #
def test_contact_export_returns_the_phone_numbers():
    """THE SILENT BUG. Export read get_contacts_full(), whose dicts carry
    {guid, name, last_online, online} and no phone at all — so item["phone"] was
    always None and every export produced an empty file with no error."""
    import asyncio
    client = _Client([_Page([_User("g1", "989120000001"),
                             _User("g2", "989120000002")])])
    phones = asyncio.run(rb.get_contact_phones(client))
    assert phones == ["989120000001", "989120000002"]


def test_get_contacts_full_still_has_no_phone_field():
    """Proves WHY the old code could never work, so nobody re-points export at it."""
    import asyncio
    client = _Client([_Page([_User("g1", "989120000001")])])
    contacts = asyncio.run(rb.get_contacts_full(client))
    assert contacts and "phone" not in contacts[0], (
        "if this ever gains a phone field, the comment on the export path is stale")


def test_phone_extraction_handles_the_shapes_rubika_uses():
    assert rb._phone_of({"phone": "0912"}) == "0912"
    assert rb._phone_of({"phone_number": "0913"}) == "0913"
    assert rb._phone_of({"user": {"phone": "0914"}}) == "0914"
    assert rb._phone_of({"name": "no phone"}) == ""


def test_exported_numbers_are_digits_only_and_deduplicated():
    import asyncio
    client = _Client([_Page([_User("g1", "+98 912 000 0001"),
                             _User("g2", "989120000001"),
                             _User("g3", "0912-000-0002")])])
    phones = asyncio.run(rb.get_contact_phones(client))
    assert phones == ["989120000001", "09120000002"]


def test_an_export_can_be_cancelled_between_pages():
    import asyncio
    client = _Client([_Page([_User(f"g{i}", f"98912000000{i}")
                             for i in range(3)])])
    phones = asyncio.run(rb.get_contact_phones(client, should_stop=lambda: True))
    assert phones == [], "a stop must be honoured before the first page"


def test_both_export_paths_use_the_phone_function():
    for name in ("worker_api.py", "rubika_panel.py"):
        body = _code(name)
        assert "get_contact_phones" in body, f"{name} must use it"
    # And neither may go back to mining a phone out of get_contacts_full.
    for name in ("worker_api.py", "rubika_panel.py"):
        for line in _src(name).splitlines():
            code = line.split("#")[0]
            assert 'item.get("phone")' not in code, (
                f"{name} is mining a phone from data that has none")


# --------------------------------------------------------------------------- #
# 2. The pip index is explicit — both references do this
# --------------------------------------------------------------------------- #
def test_the_dockerfile_names_the_package_index():
    """A server can ship a pip.conf pointing at a local mirror, and a mirror that
    serves the sdist makes pip compile telethon from source — thousands of files,
    which is what filled a disk mid-build."""
    body = _code("Dockerfile")
    assert "-i https://pypi.org/simple" in body
    assert "--prefer-binary" in body


# --------------------------------------------------------------------------- #
# 3. The branch a worker clones must contain the code
# --------------------------------------------------------------------------- #
def test_the_default_branch_is_not_the_readme_only_main():
    """origin/main carries one commit with only README.md. A worker built from it
    clones a repo with no Dockerfile — a failure that looks like anything else."""
    assert config.GIT_BRANCH != "main"
    assert config.GIT_BRANCH, "a worker must have a branch to clone"
    # Also assert on the SOURCE. config is imported once per session, so a stale
    # module object can hide a changed default — mutation testing found this test
    # passing while the default had been put back to "main".
    body = _code("config.py")
    assert '"GIT_BRANCH", "main"' not in body, (
        'main holds only a README; a worker cloned from it has no Dockerfile')


def test_the_env_template_pins_the_working_branch():
    body = _code("deploy/env.template")
    assert "GIT_BRANCH=feat/multi-tenant-foundation" in body


# --------------------------------------------------------------------------- #
# 4. A full disk and a transient registry error — neither reference handled these
# --------------------------------------------------------------------------- #
def test_disk_space_is_checked_before_the_build():
    """A build needs room for the base image, the wheels and pip's temp files. One
    report died with "[Errno 28] No space left on device" deep inside a pip build,
    ten minutes in."""
    body = _code("worker.py")
    start = body.index("async def provision_worker")
    section = body[start:start + 12000]
    assert "df -Pm" in section, "free space must be measured"
    assert "WORKER_MIN_DISK_MB" in section
    assert section.index("df -Pm") < section.index("docker build"), (
        "the check must come before the build, not after it fails")


def test_a_low_disk_triggers_a_prune_before_giving_up():
    """Failed builds leave dangling layers; several attempts add up. Reclaiming our
    own leftovers is worth trying before telling the owner to resize a disk."""
    body = _code("worker.py")
    assert "docker system prune -af" in body


def test_the_minimum_disk_is_realistic():
    assert config.WORKER_MIN_DISK_MB >= 1500


@pytest.mark.parametrize("output", [
    "502 Bad Gateway",
    "httpReadSeeker: failed open: unexpected status from GET request",
    "unknown: failed to copy: ...",
    "TLS handshake timeout",
    "connection reset by peer",
])
def test_transient_registry_errors_are_recognised(output):
    """Docker Hub answers 502 often enough to matter. A working server reported as
    a defeat is a bad outcome."""
    assert worker._is_transient_build_error(output) is True


@pytest.mark.parametrize("output", [
    "No space left on device",
    "Dockerfile parse error",
    "returned a non-zero code: 1",
])
def test_real_failures_are_not_retried(output):
    """Retrying a full disk or a broken Dockerfile just wastes ten more minutes."""
    assert worker._is_transient_build_error(output) is False


def test_a_full_disk_beats_a_copy_failure_in_the_verdict():
    """Both can appear together; treating it as transient would bury the real
    cause behind three pointless retries."""
    mixed = "failed to copy: something\nOSError: [Errno 28] No space left on device"
    assert worker._is_transient_build_error(mixed) is False


def test_the_build_is_retried():
    body = _code("worker.py")
    assert "WORKER_BUILD_ATTEMPTS" in body
    assert config.WORKER_BUILD_ATTEMPTS >= 2


def test_a_build_failure_is_diagnosed_not_dumped():
    """One report dumped 1500 characters of pip traceback with "No space left on
    device" buried in the middle."""
    body = _code("worker.py")
    assert "_explain_setup_failure(out, err, ip)" in body
    diagnosis = worker._explain_setup_failure(
        "", "OSError: [Errno 28] No space left on device", "1.2.3.4")
    assert "دیسک" in diagnosis
    assert "df -h" in diagnosis


def test_a_transient_failure_says_it_is_not_the_owners_fault():
    """After the retries are spent, the message should still distinguish "Docker
    Hub was down" from "your server is broken"."""
    body = _code("worker.py")
    assert "مشکل سرور تو نیست" in body
