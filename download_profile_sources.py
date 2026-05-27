import argparse
import fnmatch
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent


def log_status(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {message}")


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Profile {path} must contain a top-level mapping.")
    return data


def is_skipped(mapping: dict | None) -> bool:
    return bool(mapping and mapping.get("skip"))


def repo_path(value: str | None, label: str, *, must_exist: bool = True) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if must_exist and not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    return path


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def normalize_patterns(download_spec: dict, label: str) -> list[str]:
    include = download_spec.get("include")
    if isinstance(include, str):
        patterns = [include]
    elif isinstance(include, list):
        patterns = include
    else:
        raise ValueError(f"{label}.include must be a string or list of strings.")

    normalized = []
    for index, pattern in enumerate(patterns):
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{label}.include[{index}] must be a non-empty string.")
        normalized.append(pattern)
    if not normalized:
        raise ValueError(f"{label}.include must not be empty.")
    return normalized


def archive_file_name(url: str, fallback_index: int) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name
    return name or f"download-{fallback_index}.zip"


def destination_has_files(path: Path) -> bool:
    return path.exists() and path.is_dir() and any(path.iterdir())


def download_archive(url: str, archive_path: Path) -> None:
    with urllib.request.urlopen(url) as response, archive_path.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def archive_member_matches(member_name: str, pattern: str) -> bool:
    return member_name == pattern or fnmatch.fnmatch(member_name, pattern)


def extract_zip_archive(archive_path: Path, destination_root: Path, patterns: list[str], *, flatten: bool) -> int:
    extracted_count = 0
    with zipfile.ZipFile(archive_path) as archive:
        members = [
            member
            for member in archive.infolist()
            if not member.is_dir() and any(archive_member_matches(member.filename, pattern) for pattern in patterns)
        ]
        if not members:
            raise FileNotFoundError(
                f"No files matched {patterns!r} in archive {archive_path.name}."
            )

        for member in members:
            relative_name = Path(member.filename).name if flatten else Path(member.filename)
            destination_path = destination_root / relative_name
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination_path.open("wb") as target:
                shutil.copyfileobj(source, target)
            extracted_count += 1
    return extracted_count


def run_downloads(profile_path: Path, profile: dict, *, force: bool, dry_run: bool) -> None:
    if is_skipped(profile):
        log_status(f"Skipping profile {profile_path.name} due to skip: true")
        return

    source_root_value = profile.get("source_root")
    if not isinstance(source_root_value, str) or not source_root_value:
        raise ValueError(f"{profile_path.name} must define a non-empty source_root for source downloads.")
    destination_root = repo_path(source_root_value, f"source_root in {profile_path.name}", must_exist=False)
    if destination_root is None:
        raise ValueError(f"{profile_path.name} must define a non-empty source_root for source downloads.")

    downloads = profile.get("downloads")
    if not isinstance(downloads, list) or not downloads:
        raise ValueError(f"{profile_path.name} must define a non-empty downloads list.")

    if destination_has_files(destination_root) and not force:
        log_status(
            f"Skipping source download for {profile_path.name} because {display_path(destination_root)} already exists."
        )
        return

    if dry_run:
        action = "Would re-download" if destination_has_files(destination_root) and force else "Would download"
        log_status(f"{action} sources for {profile_path.name} into {display_path(destination_root)}")
        for index, download_spec in enumerate(downloads):
            if not isinstance(download_spec, dict):
                raise ValueError(f"downloads[{index}] in {profile_path.name} must be a mapping.")
            url = download_spec.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError(f"downloads[{index}].url in {profile_path.name} must be a non-empty string.")
            patterns = normalize_patterns(download_spec, f"downloads[{index}] in {profile_path.name}")
            print(f"- {url}")
            for pattern in patterns:
                print(f"  - include: {pattern}")
        return

    if destination_root.exists() and force:
        log_status(f"Removing existing source directory {display_path(destination_root)} due to --force")
        shutil.rmtree(destination_root)
    destination_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="zevcode-source-download-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        for index, download_spec in enumerate(downloads):
            if not isinstance(download_spec, dict):
                raise ValueError(f"downloads[{index}] in {profile_path.name} must be a mapping.")

            url = download_spec.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError(f"downloads[{index}].url in {profile_path.name} must be a non-empty string.")
            patterns = normalize_patterns(download_spec, f"downloads[{index}] in {profile_path.name}")
            flatten = bool(download_spec.get("flatten", True))
            archive_path = temp_dir / archive_file_name(url, index)

            log_status(f"Downloading {url}")
            download_archive(url, archive_path)
            log_status(
                f"Extracting {archive_path.name} into {display_path(destination_root)} "
                f"with include={patterns}"
            )
            extracted_count = extract_zip_archive(archive_path, destination_root, patterns, flatten=flatten)
            log_status(f"Extracted {extracted_count} file(s) from {archive_path.name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="download_profile_sources.py",
        description="Download and extract upstream source fonts defined by a profile YAML file.",
    )
    parser.add_argument("profile", help="YAML profile path.")
    parser.add_argument("-f", "--force", action="store_true", help="Re-download even when the destination already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be downloaded without changing files.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    profile_path = repo_path(args.profile, "Profile file")
    if profile_path is None:
        raise ValueError("Profile path is required.")
    profile = load_yaml(profile_path)
    run_downloads(profile_path, profile, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
