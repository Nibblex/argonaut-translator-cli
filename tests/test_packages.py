import types

import pytest

from argonaut import packages
from argonaut.translation import CancelledError


def fake_pkg(tmp_path=None, from_code="en", to_code="es", from_name="English",
             to_name="Spanish", type="translate", content=b"model bytes"):
    links = []
    if tmp_path is not None:
        archive = tmp_path / f"{from_code}_{to_code}.argosmodel"
        archive.write_bytes(content)
        links = [archive.as_uri()]
    return types.SimpleNamespace(
        type=type,
        from_code=from_code,
        to_code=to_code,
        from_name=from_name,
        to_name=to_name,
        links=links,
    )


@pytest.fixture(autouse=True)
def no_real_packages(monkeypatch):
    """install() uninstalls the previous version of the pair, so an
    unpatched lookup here would delete real packages from the machine
    running the tests. Tests that need one override this."""
    monkeypatch.setattr(packages.argos_package, "get_installed_packages", lambda: [])
    monkeypatch.setattr(
        packages.argos_package,
        "uninstall",
        lambda pkg: pytest.fail("uninstalled a real package"),
    )


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch):
    target = tmp_path / "downloads"
    monkeypatch.setattr(packages.argos_settings, "downloads_dir", target)
    return target


def test_pair_and_installed_versions(monkeypatch):
    pkg = fake_pkg()
    pkg.package_version = "1.3"
    assert packages.pair(pkg) == ("en", "es")
    monkeypatch.setattr(
        packages.argos_package, "get_installed_packages", lambda: [pkg]
    )
    assert packages.installed_versions() == {("en", "es"): "1.3"}


def test_version_comparison():
    assert packages.is_newer("1.3", "1.0")
    assert packages.is_newer("1.10", "1.9")  # compared as numbers, not text
    assert packages.is_newer("2.0", "1.9.9")
    assert not packages.is_newer("1.3", "1.3")
    assert not packages.is_newer("1.0", "1.3")
    assert not packages.is_newer("", "1.0")  # unparsable never wins


def test_download_reports_progress(tmp_path, downloads_dir):
    pkg = fake_pkg(tmp_path)
    progress = []
    path = packages.download(
        pkg, on_progress=lambda done, total: progress.append((done, total))
    )
    assert path.startswith(str(downloads_dir))
    with open(path, "rb") as f:
        assert f.read() == b"model bytes"
    done, total = progress[-1]
    assert done == total == len(b"model bytes")


def test_download_sends_the_argos_user_agent(tmp_path, downloads_dir, monkeypatch):
    pkg = fake_pkg(tmp_path)
    requests = []
    real_urlopen = packages.urllib.request.urlopen

    def spy_urlopen(request):
        requests.append(request)
        return real_urlopen(request)

    monkeypatch.setattr(packages.urllib.request, "urlopen", spy_urlopen)
    packages.download(pkg)
    assert requests[0].get_header("User-agent") == packages.USER_AGENT


def test_download_falls_back_to_the_next_link(tmp_path, downloads_dir):
    pkg = fake_pkg(tmp_path)
    pkg.links.insert(0, (tmp_path / "missing.argosmodel").as_uri())
    path = packages.download(pkg)
    with open(path, "rb") as f:
        assert f.read() == b"model bytes"


def test_download_raises_when_every_link_fails(tmp_path, downloads_dir):
    pkg = fake_pkg(tmp_path)
    pkg.links = [(tmp_path / "missing.argosmodel").as_uri()]
    with pytest.raises(OSError):
        packages.download(pkg)


def test_get_size_reads_content_length(tmp_path):
    pkg = fake_pkg(tmp_path)
    assert packages.get_size(pkg) == len(b"model bytes")


def test_get_size_returns_zero_when_unreachable(tmp_path):
    pkg = fake_pkg(tmp_path)
    pkg.links = [(tmp_path / "missing.argosmodel").as_uri()]
    assert packages.get_size(pkg) == 0
    pkg.links = []
    assert packages.get_size(pkg) == 0


def test_download_cancel_removes_partial_file(tmp_path, downloads_dir):
    pkg = fake_pkg(tmp_path)
    with pytest.raises(CancelledError):
        packages.download(pkg, is_cancelled=lambda: True)
    assert not any(downloads_dir.iterdir())


def test_install_downloads_installs_and_cleans_up(tmp_path, downloads_dir, monkeypatch):
    pkg = fake_pkg(tmp_path)
    installed = []
    monkeypatch.setattr(
        packages.argos_package,
        "install_from_path",
        lambda path: installed.append(str(path)),
    )
    packages.install(pkg)
    assert len(installed) == 1
    assert not any(downloads_dir.iterdir())  # the archive is removed


def test_install_replaces_an_older_version(tmp_path, downloads_dir, monkeypatch):
    """Argos extracts into a directory named after the pair, so upgrading
    must drop the old version instead of unpacking on top of it."""
    pkg = fake_pkg(tmp_path)
    old = fake_pkg()  # same pair, already installed
    calls = []
    monkeypatch.setattr(
        packages.argos_package, "get_installed_packages", lambda: [old]
    )
    monkeypatch.setattr(
        packages.argos_package, "uninstall", lambda p: calls.append(("uninstall", p))
    )
    monkeypatch.setattr(
        packages.argos_package,
        "install_from_path",
        lambda path: calls.append(("install", path)),
    )
    packages.install(pkg)
    assert [step for step, _ in calls] == ["uninstall", "install"]
    assert calls[0][1] is old


def test_install_removes_archive_on_failure(tmp_path, downloads_dir, monkeypatch):
    pkg = fake_pkg(tmp_path)

    def broken_install(path):
        raise RuntimeError("corrupt archive")

    monkeypatch.setattr(packages.argos_package, "install_from_path", broken_install)
    with pytest.raises(RuntimeError):
        packages.install(pkg)
    assert not any(downloads_dir.iterdir())


def test_uninstall_removes_only_the_matching_pair(monkeypatch):
    en_es, en_fr = fake_pkg(), fake_pkg(to_code="fr")
    removed = []
    monkeypatch.setattr(
        packages.argos_package, "get_installed_packages", lambda: [en_es, en_fr]
    )
    monkeypatch.setattr(
        packages.argos_package, "uninstall", lambda pkg: removed.append(pkg)
    )
    packages.uninstall(("en", "fr"))
    assert removed == [en_fr]


def test_get_available_filters_and_sorts(tmp_path, monkeypatch):
    index = tmp_path / "index.json"
    index.write_text("[]")
    monkeypatch.setattr(packages.argos_package, "update_package_index", lambda: None)
    monkeypatch.setattr(packages.argos_settings, "local_package_index", index)
    monkeypatch.setattr(
        packages.argos_package,
        "get_available_packages",
        lambda: [
            fake_pkg(from_code="es", to_code="en", from_name="Spanish", to_name="English"),
            fake_pkg(type="sbd"),
            fake_pkg(from_code="en", to_code=None),
            fake_pkg(from_code="en", to_code="fr", to_name="French"),
            fake_pkg(from_code="en", to_code="es"),
        ],
    )
    available = packages.get_available()
    assert [(p.from_name, p.to_name) for p in available] == [
        ("English", "French"),
        ("English", "Spanish"),
        ("Spanish", "English"),
    ]


def test_get_available_raises_without_index(tmp_path, monkeypatch):
    monkeypatch.setattr(packages.argos_package, "update_package_index", lambda: None)
    monkeypatch.setattr(
        packages.argos_settings, "local_package_index", tmp_path / "missing.json"
    )
    with pytest.raises(RuntimeError):
        packages.get_available()
