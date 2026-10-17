import getpass
import os
import sys
from pathlib import Path

from .config import ZotliteError

try:
    import paramiko
except ImportError:
    print("Error: paramiko is required for SSH support. Reinstall with: pipx install .", file=sys.stderr)
    sys.exit(1)


class Storage:
    """Where a Zotero library lives: gives local paths to its database and PDFs."""

    db_path: Path

    @staticmethod
    def open(library_path, cache_dir):
        if library_path.startswith("ssh://"):
            return RemoteStorage.from_url(library_path, cache_dir)

        return LocalStorage(Path(library_path).expanduser())

    def pdf_path(self, key, filename):
        """Local path of the attachment `filename` stored under `key`."""
        raise NotImplementedError

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class LocalStorage(Storage):
    def __init__(self, zotero_path):
        self.zotero_path = zotero_path
        self.db_path     = zotero_path / 'zotero.sqlite'

        if not self.db_path.exists():
            raise ZotliteError(f"Database not found at {self.db_path}")

    def pdf_path(self, key, filename):
        path = self.zotero_path / 'storage' / key / filename

        if not path.exists():
            raise ZotliteError("PDF file not found locally.")

        return path


class RemoteStorage(Storage):
    def __init__(self, hostname, username, remote_zotero_path, cache_dir):
        self.remote_zotero_path = remote_zotero_path
        self.cache_dir          = cache_dir
        self.client             = self._connect(hostname, username)
        self.sftp               = self.client.open_sftp()
        self.db_path            = cache_dir / "zotero.sqlite"

        cache_dir.mkdir(exist_ok=True)
        self._sync_db()

    @classmethod
    def from_url(cls, url, cache_dir):
        path_without_scheme = url[6:]
        user_host, remote_zotero_path = path_without_scheme.split('/', 1)
        remote_zotero_path = '/' + remote_zotero_path

        if '@' in user_host:
            username, hostname = user_host.split('@', 1)

        else:
            username = None
            hostname = user_host

        hostname = hostname.replace(":", "")

        return cls(hostname, username, remote_zotero_path, cache_dir)

    @staticmethod
    def _connect(hostname, username=None):
        ssh_config_file = Path.home() / ".ssh" / "config"
        ssh_config = paramiko.SSHConfig()

        if ssh_config_file.exists():
            with open(ssh_config_file) as f:
                ssh_config.parse(f)

        host_conf = ssh_config.lookup(hostname)
        host_name = host_conf.get('hostname', hostname)
        host_port = int(host_conf.get('port', 22))

        connect_kwargs = { 'hostname' : host_name
                         , 'port'     : host_port
                         }

        if username:
            connect_kwargs['username'] = username
        elif 'user' in host_conf:
            connect_kwargs['username'] = host_conf['user']
        else:
            connect_kwargs['username'] = getpass.getuser()

        if 'identityfile' in host_conf:
            connect_kwargs['key_filename'] = host_conf['identityfile']

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        print(f"Connecting to SSH -> {connect_kwargs['username']}@{connect_kwargs['hostname']}...", file=sys.stderr)

        try:
            client.connect(**connect_kwargs)

        except paramiko.ssh_exception.AuthenticationException:
            password = getpass.getpass(f"Password for {connect_kwargs['username']}@{connect_kwargs['hostname']}: ")
            connect_kwargs['password'] = password
            client.connect(**connect_kwargs)

        return client

    def _remote(self, *parts):
        return "/".join([self.remote_zotero_path, *parts]).replace("//", "/")

    def _sync_db(self):
        remote_db = self._remote("zotero.sqlite")

        print("Syncing database...", file=sys.stderr)

        try:
            self.sftp.get(remote_db, str(self.db_path))

        except IOError:
            raise ZotliteError(f"Database not found on remote server at {remote_db}")

    def pdf_path(self, key, filename):
        remote_pdf = self._remote("storage", key, filename)
        local_pdf  = self.cache_dir / key / filename

        try:
            remote_stat = self.sftp.stat(remote_pdf)

        except IOError:
            raise ZotliteError("PDF not found on remote server.")

        # Re-use the cached copy unless the remote file changed
        if local_pdf.exists():
            local_stat = local_pdf.stat()

            if local_stat.st_size == remote_stat.st_size and int(local_stat.st_mtime) == int(remote_stat.st_mtime):
                return local_pdf

        local_pdf.parent.mkdir(exist_ok=True)

        try:
            self.sftp.get(remote_pdf, str(local_pdf))

        except IOError:
            raise ZotliteError("PDF not found on remote server.")

        os.utime(local_pdf, (remote_stat.st_atime, remote_stat.st_mtime))

        return local_pdf

    def close(self):
        self.sftp.close()
        self.client.close()
