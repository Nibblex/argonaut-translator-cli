"""Discovery, download and installation of Argos Translate language
packages, so language pairs can be installed from the GUI instead of
the argospm command line."""

import os
import urllib.request

from argostranslate import package as argos_package
from argostranslate import settings as argos_settings

from argonaut.translation import CancelledError

# argos-net.com answers 403 Forbidden to urllib's default agent
USER_AGENT = "ArgosTranslate"


def pair(pkg):
    """The (from, to) language codes identifying a package."""
    return (pkg.from_code, pkg.to_code)


def installed_versions():
    """Version of the installed package for each (from, to) language pair."""
    return {
        pair(pkg): pkg.package_version
        for pkg in argos_package.get_installed_packages()
    }


def version_tuple(version):
    """Comparable form of a version string like "1.3"; non-numeric parts
    count as zero so a malformed version never sorts above a real one."""
    return tuple(
        int(part) if part.isdigit() else 0 for part in str(version).split(".")
    )


def is_newer(candidate, current):
    return version_tuple(candidate) > version_tuple(current)


def get_available():
    """Downloads the remote package index and returns the translation
    packages sorted by language pair. Raises if the index has never been
    fetched and cannot be downloaded now."""
    argos_package.update_package_index()
    # update_package_index swallows network errors; without a local copy
    # get_available_packages would retry it in an endless loop
    if not os.path.exists(argos_settings.local_package_index):
        raise RuntimeError("could not download the package index")
    packages = [
        pkg
        for pkg in argos_package.get_available_packages()
        if pkg.type == "translate" and pkg.from_code and pkg.to_code
    ]
    packages.sort(key=lambda p: (p.from_name, p.to_name))
    return packages


def open_first_link(links):
    """Opens the first reachable of a package's mirror links."""
    last_error = None
    for url in links:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            return urllib.request.urlopen(request)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise last_error


def get_size(pkg):
    """Remote size in bytes of a package archive, from a HEAD request to
    the first reachable mirror; 0 if it cannot be determined."""
    for url in pkg.links:
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        try:
            response = urllib.request.urlopen(request)
        except Exception:  # noqa: BLE001
            continue
        try:
            return int(response.headers.get("Content-Length") or 0)
        finally:
            response.close()
    return 0


def download(pkg, on_progress=None, is_cancelled=None):
    """Downloads a package to Argos's downloads folder, reporting
    (done_bytes, total_bytes) after each chunk, and returns the file path.
    Cancelling or failing removes the partial file."""
    on_progress = on_progress or (lambda done, total: None)
    is_cancelled = is_cancelled or (lambda: False)
    filename = argos_package.argospm_package_name(pkg) + ".argosmodel"
    target = os.path.join(argos_settings.downloads_dir, filename)
    os.makedirs(argos_settings.downloads_dir, exist_ok=True)

    response = open_first_link(pkg.links)
    total = int(response.headers.get("Content-Length") or 0)
    done = 0
    try:
        with open(target, "wb") as out:
            while True:
                if is_cancelled():
                    raise CancelledError()
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                on_progress(done, total)
    except BaseException:
        if os.path.exists(target):
            os.remove(target)
        raise
    finally:
        response.close()
    return target


def uninstall(target_pair):
    """Uninstalls the installed package for a (from, to) language pair.
    argostranslate clears its language cache itself."""
    for pkg in argos_package.get_installed_packages():
        if pair(pkg) == target_pair:
            argos_package.uninstall(pkg)


def install(pkg, on_progress=None, is_cancelled=None):
    """Downloads and installs a package, removing the downloaded archive.
    An older version of the same pair is uninstalled first: Argos extracts
    into a directory named after the pair only, so upgrading in place would
    leave the previous version's files behind. install_from_path clears
    argostranslate's language cache itself."""
    path = download(pkg, on_progress, is_cancelled)
    try:
        uninstall(pair(pkg))  # no-op on a fresh install
        argos_package.install_from_path(path)
    finally:
        os.remove(path)
