import requests
import logging
import sys
import re
import os
from io import BytesIO
from git import Repo, Tag
from github import Github
from github.GithubException import UnknownObjectException
from hashlib import sha256
from time import perf_counter

log = logging.getLogger(__name__)
logging.basicConfig(level='INFO')

RELEASES_URL = 'https://downloads.raspberrypi.org/operating-systems-categories.json'
RELEASE_NOTES_URL = 'https://downloads.raspberrypi.org/raspios_lite_armhf/release_notes.txt'
RELEASE_SLUG = 'raspberry-pi-os-32-bit'
RELEASE_TITLE_SUFFIX = 'Lite'

def die(msg, code=1):
    log.critical(msg)
    sys.exit(code)

class DownloadBuffer():
    """Downloads a file in chunks as they are read, and produces a hash as it goes.
       Used for downloading and uploading the image in memory,"""
    
    def __init__(self, url):
        self._url = url
        self._size = 0
        self._chunk_iter = None
        self._buff = b''
        self._hasher = sha256()
        self._progress = 0
        self._last_progress = 0

    def read(self, size):
        if not self._chunk_iter:
            self._read_start = perf_counter()
            response = requests.get(self._url, stream=True)
            self._size = int(response.headers['content-length'])
            self._chunk_iter = response.iter_content(size)
            self._progress = 0
        def report(size):
            new_prog = min(self._progress + (size / self._size * 100), 100)
            if new_prog - self._last_progress >= 1:
                log.info(f'Transferred so far: {new_prog:.2f}%')
                self._last_progress = new_prog
            self._progress = new_prog
        if size < 0:
            buff = b''.join(self._chunk_iter)
            report(len(buff))
            return buff
        if len(self._buff) >= size:
            return_buff = self._buff[:size]
            self._buff = self._buff[size:]
            report(len(return_buff))
            return return_buff
        while len(self._buff) < size:
            next_chunk = next(self._chunk_iter, b'')
            if not next_chunk:
                report(len(self._buff))
                return self._buff
            self._hasher.update(next_chunk)
            self._buff += next_chunk
        return self.read(size)

    def digest(self):
        return self._hasher.hexdigest()


GITHUB_TOKEN = os.environ['GITHUB_TOKEN']
GITHUB_REPOSITORY = os.environ['GITHUB_REPOSITORY']

log.info('Fetching latest release info...')

releases = requests.get(RELEASES_URL).json()
images = next(( r for r in releases if r['slug'] == RELEASE_SLUG ))['images']
release = next(( r for r in images if r['title'].endswith(RELEASE_TITLE_SUFFIX)))

new_version = release['releaseDate']
download_url = release['urlHttp']
download_size = release['size']
download_hash = release['sha256']

repo = Repo('.')

log.info('Ensuring VERSION file is up to date...')

try:
    with open('VERSION', 'r') as f:
        repo_version = f.read().strip()
except FileNotFoundError:
    repo_version = ''
    pass

if new_version < repo_version:
    die('The local version is newer (!!)', code=0)

repo_changed = False
if new_version > repo_version:
    log.info('Local VERSION file needs update')
    with open('VERSION', 'w') as f:
        f.write(new_version)
    repo_changed = True
    repo.index.add('VERSION')
    repo.index.commit(f'Update version to {new_version}')
    repo.remotes.origin.push()

log.info('Ensuring tag exists...')

semver = 'v' + re.sub(r'-0?', '.', new_version)
try:
    tag: Tag = repo.tags[semver]
except:
    tag = None
if tag and repo_changed:
    die('Tag already exists but the file wasn\'t updated (!!)')
if not tag:
    tag = repo.create_tag(semver)
    repo.remotes.origin.push(tags=True)

log.info('Ensuring release exists...')

gh = Github(GITHUB_TOKEN)
gh_repo = gh.get_repo(GITHUB_REPOSITORY)
try:
    release = gh_repo.get_release(semver)
except UnknownObjectException:
    release = None
if not release:
    release_notes_lines = requests.get(RELEASE_NOTES_URL).text.splitlines()
    release_notes = ''
    if release_notes_lines.pop(0).strip().startswith(new_version):
        while release_notes_lines:
            line = release_notes_lines.pop(0)
            if not line.startswith(' '):
                break
            release_notes += line + '\n'
    release = gh_repo.create_git_release(semver, new_version, release_notes)

assets = list(release.get_assets())

image_asset = next(( a for a in assets if a.content_type == 'application/zip' ), None)
if not image_asset:
    log.info('Creating image asset...')
    download_buffer = DownloadBuffer(download_url)
    image_asset = release.upload_asset_from_memory(download_buffer, download_size, 'image.zip', 'application/zip')
    if download_buffer.digest() != download_hash:
        image_asset.delete_asset()
        die('Downloaded image doesn\'t match its hash')

hash_asset = next(( a for a in assets if a.name.endswith('sha256') ), None)
if not hash_asset:
    log.info('Creating hash asset...')
    hash_str = f'{download_hash}\timage.zip\n'.encode()
    release.upload_asset_from_memory(BytesIO(hash_str), len(hash_str), 'image.zip.sha256', 'text/plain')

