#! /usr/bin/env python3


from argparse import ArgumentParser, Namespace
from os import getcwd, getenv, listdir, makedirs, name, path, remove, rmdir
from sys import exit, stdout, stderr
from typing import Any, Iterator, NoReturn, TextIO
from types import FrameType
from itertools import count
from requests import Session, Response, Timeout
from requests.structures import CaseInsensitiveDict
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from hashlib import sha256
from shutil import move
from signal import signal, SIGINT, SIG_IGN
from time import perf_counter, sleep, time
from re import sub


NEW_LINE: str = "\n" if name != "nt" else "\r\n"


def has_ansi_support() -> bool:
    """
    has_ansi_support

    Checks whether the platform support ansi or not.

    :return: True if the platform supports it.
    """

    import os
    import sys

    if not sys.stdout.isatty():
        return False

    if os.name == "nt":
        # Not sure, but I think the console on win10+ have default ansi support
        return sys.getwindowsversion().major >= 10

    # I hope the rest supports it??
    return True


# I hope these 100 character are enough for fallback,
# anyone using win7 still?
TERMINAL_CLEAR_LINE: str = f"\r{' ' * 100} \r" if not has_ansi_support() else "\033[2K\r"
ANSI_ENABLED: bool = has_ansi_support()
ANSI_RESET: str = "\033[0m" if ANSI_ENABLED else ""
ANSI_DIM: str = "\033[2m" if ANSI_ENABLED else ""
ANSI_CYAN: str = "\033[36m" if ANSI_ENABLED else ""
ANSI_GREEN: str = "\033[32m" if ANSI_ENABLED else ""
ANSI_YELLOW: str = "\033[33m" if ANSI_ENABLED else ""
ANSI_RED: str = "\033[31m" if ANSI_ENABLED else ""


def _color(text: str, color: str) -> str:
    if not ANSI_ENABLED:
        return text
    return f"{color}{text}{ANSI_RESET}"


def _print(msg: str, error: bool = False) -> None:
    """
    _print

    Print a message.

    :param msg: a string to be printed.
    :param error: if the error stream output should be used instead of the standard output.
    :return:
    """

    output: TextIO = stderr if error else stdout
    sensitive_key: str = "pass" + "word="
    safe_msg: str = sub(rf"({sensitive_key})[^&\s]+", r"\1***", msg)
    safe_msg = sub(r"(accountToken=)[^;\s]+", r"\1***", safe_msg)
    output.write(safe_msg)
    output.flush()


def _print_status(status: str, filename: str, details: str = "") -> None:
    """
    _print_status

    Prints a concise status line for file-level operations.

    :param status: short status label.
    :param filename: file name being handled.
    :param details: optional extra details.
    :return:
    """

    suffix: str = f" - {details}" if details else ""
    status_colors: dict[str, str] = {
        "COMPLETE": ANSI_GREEN,
        "SKIP": ANSI_CYAN,
        "RESUME": ANSI_CYAN,
        "VERIFY": ANSI_YELLOW,
        "RESTART": ANSI_YELLOW,
        "FAILED": ANSI_RED,
    }
    label: str = _color(f"[{status}]", status_colors.get(status, ANSI_DIM))
    _print(f"{TERMINAL_CLEAR_LINE}{label} {filename}{suffix}{NEW_LINE}")


def _print_banner() -> None:
    title: str = _color("GOFILE DOWNLOADER", ANSI_CYAN)
    _print(f"{ANSI_DIM}{'-' * 28}{ANSI_RESET}{NEW_LINE}")
    _print(f"{title}{NEW_LINE}")
    _print(f"{ANSI_DIM}{'-' * 28}{ANSI_RESET}{NEW_LINE}")


def die(msg: str) -> NoReturn:
    """
    die

    Display a message of error and exit.

    :param msg: a string to be printed.
    :return:
    """

    _print(f"{msg}{NEW_LINE}", True)
    exit(-1)


def generate_website_token(user_agent: str, account_token: str) -> str:
    """
    generate_website_token

    Generates the dynamic X-Website-Token required by GoFile API.
    """
    time_slot = int(time()) // 14400
    raw = f"{user_agent}::en-US::{account_token}::{time_slot}::9844d94d963d30"
    return sha256(raw.encode()).hexdigest()


class Downloader:
    def __init__(
        self,
        root_dir: str,
        interactive: bool,
        max_workers: int,
        number_retries,
        timeout: float,
        chunk_size: int,
        stop_event: Event,
        session: Session,
        url: str,
        password: str | None = None,
        speed_limit: int = 0,
    ) -> None:
        """
        Downloader

        Downloader class to concurrently manage, download and write files to disk.
        This one does the heavy lifting, the actual working of downloading.

        :root_dir: Directory where files will be saved (defaults to current directory).
        :interactive: Whether download will be interactive or not
                      (it's disabled by default while batch downloading from a text file)
        :max_workers: Maximum number of concurrent workers (tasks).
        :number_retries: The maximum number of connections retries for POST and GET requests.
        :timeout: Maximum number of time to wait until give up on trying to establish a connection.
        :chunk_size: Maximum chunk byte size.
        :stop_event: An Event object to handle the request to stop the program and exit gracefully.
        :session: Session object to handle headers, cookies and allowing reuse of resources and TCP connections.
        :url: The content url to download.
        :password: The content password if it's protected.
        """

        # Dictionary to hold information about file and its directories structure
        # {"index": {"path": "", "filename": "", "link": ""}}
        # where the largest index is the top most file
        self._files_info: dict[str, dict[str, str]] = {}

        self._max_workers: int = max_workers
        self._number_retries: int = number_retries
        self._timeout: float = timeout
        self._interactive: bool = interactive
        self._chunk_size: int = chunk_size
        self._password: str | None = password
        self._speed_limit: int = speed_limit if speed_limit > 0 else 0
        self._session: Session = session
        self._stop_event: Event = stop_event
        self._root_dir: str = root_dir
        self._url: str = url


    def run(self) -> None:
        """
        run

        Requests to start downloading files.

        :return:
        """

        try:
            if not self._url.split("/")[-2] == "d":
                _print(f"The url probably doesn't have an id in it: {self._url}.{NEW_LINE}")
                return

            content_id: str = self._url.split("/")[-1]
        except IndexError:
            _print(f"{self._url} doesn't seem a valid url.{NEW_LINE}")
            return

        _password: str | None = sha256(self._password.encode()).hexdigest() if self._password else None

        content_dir: str = path.join(self._root_dir, content_id)
        self._build_content_tree_structure(content_dir, content_id, _password)

        # removes the root content directory if there's no file or subdirectory
        if path.exists(content_dir) and not listdir(content_dir) and not self._files_info:
            _print(f"Empty directory for url: {self._url}, nothing done.{NEW_LINE}")
            self._remove_dir(content_dir)
            return

        if self._interactive:
            self._do_interactive(content_dir)

        self._threaded_downloads()


    def _get_response(self, **kwargs: Any) -> Response | None:
        """
        _get_response

        Auxiliary function for the requests.session.get.

        :param kwargs: arguments for the requests.session.get function.
        :return: requests.Response or None on requests.Timeout.
        """

        for _ in range(self._number_retries):
            try:
                return self._session.get(timeout=self._timeout, **kwargs)
            except Timeout:
                continue


    def _threaded_downloads(self) -> None:
        """
        _threaded_downloads

        Parallelize the downloads.

        :return:
        """

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            for item in self._files_info.values():
                if self._stop_event.is_set():
                    return

                executor.submit(self._download_content, item)


    @staticmethod
    def _create_dirs(dirname: str) -> None:
        """
        _create_dirs

        Creates a directory and its subdirectories recursively if they don't exist.

        :param dirname: name of the directory to be created.
        :return:
        """

        makedirs(dirname, exist_ok = True)


    @staticmethod
    def _remove_dir(dirname: str) -> None:
        """
        _remove_dir

        Removes a directory if it's empty ignoring any throw.

        :param dirname: name of the directory to be created.
        :return:
        """

        try:
            rmdir(dirname)
        except:
            pass


    def _download_content(self, file_info: dict[str, str]) -> None:
        """
        _download_content

        Requests the contents of the file and writes it.

        :param file_info: a dictionary with information about a file to be downloaded.
        :return:
        """

        filename: str = file_info["filename"]
        filepath: str = path.join(file_info["path"], filename)
        tmp_file: str =  f"{filepath}.part"
        url: str = file_info["link"]
        expected_size: int | None = self._parse_expected_size(file_info.get("size"))

        if path.isfile(filepath):
            final_size: int = int(path.getsize(filepath))
            if self._is_complete_file(final_size, expected_size):
                _print_status("SKIP", filename, "already complete")
                return
            move(filepath, tmp_file)
            _print_status("VERIFY", filename, "existing final file moved to .part")

        if path.isfile(tmp_file):
            part_size: int = int(path.getsize(tmp_file))
            part_action: str = self._evaluate_partial_size(part_size, expected_size)
            if part_action == "restart":
                remove(tmp_file)
                _print_status("RESTART", filename, ".part larger than expected size")
            elif part_action == "complete":
                move(tmp_file, filepath)
                _print_status("COMPLETE", filename, "validated existing .part and finalized")
                return
            elif part_action == "resume":
                _print_status("RESUME", filename, f"from byte {part_size}")

        for _ in range(self._number_retries):
            try:
                part_size: int = 0
                headers: dict[str, str] = {}
                if path.isfile(tmp_file):
                    part_size = int(path.getsize(tmp_file))
                    headers = {"Range": f"bytes={part_size}-"}

                has_size, should_restart = self._perform_download(
                    file_info,
                    url,
                    tmp_file,
                    headers,
                    part_size
                )
            except Timeout:
                continue
            else:
                if should_restart:
                    if path.isfile(tmp_file):
                        remove(tmp_file)
                    _print_status("RESTART", filename, "server ignored Range, downloading from byte 0")
                    continue

                if has_size:
                    if self._finalize_download(file_info, tmp_file, int(has_size)):
                        return
                continue

        _print_status("FAILED", filename, "download did not complete, kept .part for retry")


    @staticmethod
    def _parse_expected_size(raw_size: str | None) -> int | None:
        """
        _parse_expected_size

        Safely parses an expected size string.

        :param raw_size: untrusted raw size value.
        :return: parsed size or None.
        """

        if not raw_size:
            return None
        try:
            parsed_size: int = int(raw_size)
            return parsed_size if parsed_size >= 0 else None
        except (TypeError, ValueError):
            return None


    @staticmethod
    def _is_complete_file(size_on_disk: int, expected_size: int | None) -> bool:
        """
        _is_complete_file

        Determines whether a local file can be considered complete.

        :param size_on_disk: local file size.
        :param expected_size: expected remote size if available.
        :return: True if validated as complete.
        """

        if expected_size is None:
            return False
        return size_on_disk >= expected_size


    @staticmethod
    def _evaluate_partial_size(part_size: int, expected_size: int | None) -> str:
        """
        _evaluate_partial_size

        Evaluates partial-file state and returns a recovery action.

        :param part_size: current temporary file size.
        :param expected_size: expected final size if available.
        :return: one of restart/complete/resume/fresh.
        """

        if part_size <= 0:
            return "fresh"
        if expected_size is None:
            return "resume"
        if part_size > expected_size:
            return "restart"
        if part_size == expected_size:
            return "complete"
        return "resume"


    def _perform_download(
        self,
        file_info: dict[str, str],
        url: str,
        tmp_file: str,
        headers: dict[str, str],
        part_size: int,
    ) -> tuple[str | None, bool]:
        """
        _perform_download

        Executes the HTTP GET request, processes file chunks, and tracks progress.

        :param file_info: a dictionary containing file details.
        :param url: the file download URL.
        :param tmp_file: temporary file path for partial downloads.
        :param headers: request headers.
        :param part_size: the current partial file size.
        :return: the total file size (if available).
        """

        if self._stop_event.is_set():
            return None, False

        response: Response | None = self._get_response(url=url, headers=headers, stream=True)

        if not response:
            _print(
                f"{TERMINAL_CLEAR_LINE}Couldn't download the file, failed to get a response from {url}.{NEW_LINE}"
            )
            return None, False

        with response:
            status_code: int = response.status_code

            if part_size > 0 and status_code == 200:
                return None, True

            if not self._is_valid_response(response.status_code, part_size):
                _print(
                    f"{TERMINAL_CLEAR_LINE}"
                    f"Couldn't download the file from {url}.{NEW_LINE}"
                    f"Status code: {status_code}{NEW_LINE}"
                )
                return None, False

            has_size: str | None = self._extract_file_size(response.headers, part_size)

            if not has_size:
                _print(
                    f"{TERMINAL_CLEAR_LINE}"
                    f"Couldn't find the file size from {url}.{NEW_LINE}"
                    f"Status code: {status_code}{NEW_LINE}"
                )
                return None, False

            self._write_chunks(
                response.iter_content(chunk_size=self._chunk_size),
                tmp_file,
                part_size,
                float(has_size),
                file_info["filename"]
            )

            return has_size, False


    @staticmethod
    def _is_valid_response(status_code: int, part_size: int) -> bool:
        """
        _is_valid_response

        Validates HTTP status code based on partial download state.

        :param status_code: the HTTP status code.
        :param part_size: the current partial file size.
        :return: True if status code is acceptable, False otherwise.
        """

        if status_code in (403, 404, 405, 500):
            return False
        if part_size == 0:
            return status_code in (200, 206)
        if part_size > 0:
            return status_code == 206
        return False


    @staticmethod
    def _extract_file_size(headers: CaseInsensitiveDict[str], part_size: int) -> str | None:
        """
        _extract_file_size

        Retrieves the file size from HTTP headers.

        :param headers: the HTTP response headers.
        :param part_size: the current partial file size.
        :return: the total file size as a string, or None if unavailable.
        """

        content_length: str | None = headers.get("Content-Length")
        content_range: str | None = headers.get("Content-Range")
        has_size: str | None = (
            content_length if part_size == 0
            else content_range.split("/")[-1] if content_range
            else None
        )

        return has_size


    def _write_chunks(
        self,
        chunks: Iterator[Any],
        tmp_file: str,
        part_size: int,
        total_size: float,
        filename: str
    ) -> None:
        """
        _write_chunks

        Iterates over download chunks and writes them to disk, updating progress.

        :param chunks: a generator of byte chunks.
        :param tmp_file: temporary file path.
        :param part_size: number of bytes already downloaded.
        :param total_size: total file size in bytes.
        :param filename: the file's name.
        :return:
        """

        start_time: float = perf_counter()

        with open(tmp_file, "ab") as f:
            downloaded: int = 0
            for chunk in chunks:
                if self._stop_event.is_set():
                    return

                f.write(chunk)
                downloaded += len(chunk)
                self._apply_speed_limit(downloaded, start_time)
                self._update_progress(filename, part_size, downloaded, total_size, start_time)


    def _apply_speed_limit(self, downloaded: int, start_time: float) -> None:
        """
        _apply_speed_limit

        Throttles download speed when speed limiting is enabled.

        :param downloaded: downloaded bytes during current request.
        :param start_time: request start time.
        :return:
        """

        if not self._speed_limit:
            return

        elapsed: float = perf_counter() - start_time
        expected_elapsed: float = downloaded / self._speed_limit
        if expected_elapsed > elapsed:
            sleep(expected_elapsed - elapsed)


    def _update_progress(
        self,
        filename: str,
        part_size: int,
        downloaded: int,
        total_size: float,
        start_time: float
    ) -> None:
        """
        _update_progress

        Calculates and displays download progress and transfer rate.

        :param filename: the name of the file being downloaded.
        :param part_size: initial file size in bytes.
        :param downloaded: downloaded bytes during current request.
        :param total_size: total file size.
        :param start_time: download start time.
        :return:
        """

        progress: float = (part_size + downloaded) / total_size * 100
        elapsed: float = max(perf_counter() - start_time, 1e-9)
        rate: float = downloaded / elapsed

        unit: str = "B/s"
        if rate < 1024:
            unit = "B/s"
        elif rate < (1024 ** 2):
            rate /= 1024
            unit = "KB/s"
        elif rate < (1024 ** 3):
            rate /= (1024 ** 2)
            unit = "MB/s"
        else:
            rate /= (1024 ** 3)
            unit = "GB/s"

        _print(
            f"{TERMINAL_CLEAR_LINE}"
            f"Downloading {filename}: {part_size + downloaded} "
            f"of {int(total_size)} {round(progress, 1)}% {round(rate, 1)}{unit}"
        )


    @staticmethod
    def _finalize_download(file_info: dict[str, str], tmp_file: str, expected_size: int) -> bool:
        """
        _finalize_download

        Verifies the final file size and moves the temporary file to its destination.

        :param file_info: a dictionary containing file details.
        :param tmp_file: temporary file path.
        :param expected_size: expected file size.
        :return: True if finalized.
        """

        if not path.isfile(tmp_file):
            return False

        size_on_disk: int = int(path.getsize(tmp_file))
        if size_on_disk == expected_size:
            _print(
                f"{TERMINAL_CLEAR_LINE}"
                f"Downloading {file_info['filename']}: {size_on_disk} "
                f"of {expected_size} Done!{NEW_LINE}"
            )
            move(tmp_file, path.join(file_info["path"], file_info["filename"]))
            _print_status("COMPLETE", file_info["filename"])
            return True
        return False


    def _register_file(
        self,
        file_index: count,
        filepath: str,
        file_url: str,
        file_size: Any = None,
    ) -> None:
        """
        _register_file

        Registers file information into the internal files info dictionary
        (with sequential index, path, filename and download url).

        :param file_index: an itertools.count object used to sequentially index discovered files.
                           Acts as a mutable counter local to the parsing thread context.
                           Should not be modified outside this function.
        :param filepath: absolute or relative path to the file on the local filesystem.
        :param file_url: remote URL link for downloading the file.
        :return:
        """

        self._files_info[str(next(file_index))] = {
            "path": path.dirname(filepath),
            "filename": path.basename(filepath),
            "link": file_url,
            "size": str(file_size) if file_size is not None else ""
        }


    @staticmethod
    def _resolve_naming_collision(
        pathing_count: dict[str, int],
        absolute_parent_dir: str,
        child_name: str,
        is_dir: bool = False,
    ) -> str:
        """
        _resolve_naming_collision

        Ensures unique file or directory paths by checking and updating a naming collision
        tracker. If a collision is detected, appends a numeric suffix to the name to
        avoid overwriting existing paths.

        :param pathing_count: dictionary used to track the number of naming collisions
                              for each path encountered during traversal.
        :param absolute_parent_dir: absolute path to the parent directory where the child
                                    (file or directory) will be created.
        :param child_name: original name of the file or directory.
        :param is_dir: boolean flag indicating whether the child is a directory, defaults to False.
        :return: a unique filepath string with a numeric suffix appended if needed.
        """

        filepath: str = path.join(absolute_parent_dir, child_name)

        if filepath in pathing_count:
            pathing_count[filepath] += 1
        else:
            pathing_count[filepath] = 0

        if pathing_count and pathing_count[filepath] > 0 and is_dir:
            return f"{filepath}({pathing_count[filepath]})"

        if pathing_count and pathing_count[filepath] > 0:
            extension: str
            root, extension = path.splitext(filepath)

            return f"{root}({pathing_count[filepath]}){extension}"

        return filepath


    def _build_content_tree_structure(
        self,
        parent_dir: str,
        content_id: str,
        password: str | None = None,
        pathing_count: dict[str, int] | None = None,
        file_index: count = count(start=0, step=1)
    ) -> None:
        """
        _build_content_tree_structure

        Recursively traverses a remote content structure and builds a corresponding
        local directory tree (handling naming collisions), while registering files url.

        :param parent_dir: absolute path to the parent directory where the current content
                           directory or file should be created.
        :param content_id: content identifier.
        :param password: optional password to access protected content.
        :param pathing_count: pointer-like dictionary used internally to track naming collisions
                              for file and directory paths. Should not be modified outside this function.
        :param file_index: an itertools.count object used to sequentially index discovered files.
                           Acts as a mutable counter local to the parsing thread context.
                           Should not be modified outside this function.
        :return:
        """

        url: str = f"https://api.gofile.io/contents/{content_id}?cache=true&sortField=createTime&sortDirection=1"

        if not pathing_count:
            pathing_count = {}

        if password:
            url = f"{url}&password={password}"

        user_agent: str = str(self._session.headers.get("User-Agent", "Mozilla/5.0"))
        auth_header: str = str(self._session.headers.get("Authorization", ""))
        account_token: str = auth_header.replace("Bearer ", "") if auth_header else ""
        wt: str = generate_website_token(user_agent, account_token)

        response: Response | None = self._get_response(
            url=url,
            headers={
                "X-Website-Token": wt,
                "X-BL": "en-US"
            }
        )
        json_response: dict[str, Any] = {} if not response else response.json()

        if not json_response or json_response["status"] != "ok":
            _print(f"Failed to fetch data response from the {url}.{NEW_LINE}")
            return

        data: dict[str, Any] = json_response["data"]

        if "password" in data and "passwordStatus" in data and data["passwordStatus"] != "passwordOk":
            _print(f"Password protected link. Please provide the password.{NEW_LINE}")
            return

        if data["type"] != "folder":
            filepath: str = self._resolve_naming_collision(pathing_count, parent_dir, data["name"])

            self._register_file(file_index, filepath, data["link"], data.get("size"))
            return

        folder_name: str = data["name"]
        absolute_path: str = self._resolve_naming_collision(pathing_count, parent_dir, folder_name)

        # If the content directory (the root directory) directory isn't named the same as the content_id,
        # use the content_id as a name for the content directory.
        #
        # Also do not use the default root directory named as "root" created by default.
        if path.basename(parent_dir) == content_id:
            absolute_path = parent_dir

        self._create_dirs(absolute_path)

        # Checks if there is any children (files and directories) and handle them
        for child in data["children"].values():
            if child["type"] == "folder":
                self._build_content_tree_structure(absolute_path, child["id"], password, pathing_count, file_index)
            else:
                filepath: str = self._resolve_naming_collision(pathing_count, absolute_path, child["name"])

                self._register_file(file_index, filepath, child["link"], child.get("size"))


    def _print_list_files(self) -> None:
        """
        _print_list_files

        Helper function to display a list of all files for selection.

        :return:
        """

        MAX_FILENAME_CHARACTERS: int = 100
        width: int = max(len(f"[{v}] -> ") for v in self._files_info.keys())

        for (k, v) in self._files_info.items():
            # Trim the filepath if it's too long
            filepath: str = path.join(v["path"], v["filename"])
            filepath = f"...{filepath[-MAX_FILENAME_CHARACTERS:]}" \
                if len(filepath) > MAX_FILENAME_CHARACTERS \
                else filepath

            text: str =  f"{f'[{k}] -> '.ljust(width)}{filepath}"

            _print(f"{text}{NEW_LINE}"
                   f"{'-' * len(text)}"
                   f"{NEW_LINE}"
            )


    def _do_interactive(self, content_dir: str) -> None:
        """
        _do_interactive

        Performs interactive file selection for download.

        :param content_dir: Content root directory.
        :return:
        """

        self._print_list_files()

        # Ensure only valid index strings are stored.
        input_list: set[str] = set(input(
            f"Files to download (Ex: 1 3 7) | or leave empty to download them all"
            f"{NEW_LINE}"
            f":: "
        ).split())
        input_list = set(self._files_info.keys()) if not input_list \
                     else input_list & set(self._files_info.keys())

        if not input_list:
            _print(f"Nothing done.{NEW_LINE}")
            self._remove_dir(content_dir)
            return

        keys_to_delete: list[str] = list(set(self._files_info.keys()) - set(input_list))

        for key in keys_to_delete:
            del self._files_info[key]



class Manager:
    def __init__(
        self,
        url_or_file: str,
        password: str | None = None,
        interactive: bool | None = None,
        max_workers: int | None = None,
        number_retries: int | None = None,
        timeout: float | None = None,
        user_agent: str | None = None,
        chunk_size: int | None = None,
        root_dir: str | None = None,
        speed_limit: int | None = None,
        batch_threads: int | None = None,
        token: str | None = None,
    ) -> None:
        """
        Manager

        Manager class to handle individual download tasks.

        :url_or_file: This may be an existent text file or url.
        :password: Password if the content is protected.
        :return:
        """

        env_root_dir: str | None = getenv("GF_DOWNLOAD_DIR")
        env_speed_limit: int = self._read_int_env("GF_SPEED_LIMIT", 0, allow_zero=True)

        # Defaults to 5 concurrent downloads
        self._max_workers: int = max_workers if max_workers is not None else self._read_int_env(
            "GF_MAX_CONCURRENT_DOWNLOADS", 5
        )
        if self._max_workers <= 0:
            self._max_workers = 5
        self._batch_threads: int = batch_threads if batch_threads is not None else self._read_int_env(
            "GF_MAX_BATCH_THREADS", self._max_workers
        )
        if self._batch_threads <= 0:
            self._batch_threads = self._max_workers
        # Defaults to 5 retries
        self._number_retries: int = number_retries if number_retries is not None else self._read_int_env(
            "GF_MAX_RETRIES", 5
        )
        if self._number_retries <= 0:
            self._number_retries = 5
        # Connection and read timeout, defaults to 15 seconds
        self._timeout: float = timeout if timeout is not None else self._read_float_env("GF_TIMEOUT", 15.0)
        if self._timeout <= 0:
            self._timeout = 15.0
        self._user_agent: str | None = user_agent if user_agent is not None else getenv("GF_USERAGENT")
        self._interactive: bool = interactive if interactive is not None else getenv("GF_INTERACTIVE") == "1"
        # The number of bytes it should read into memory
        self._chunk_size: int = chunk_size if chunk_size is not None else self._read_int_env("GF_CHUNK_SIZE", 2097152)
        if self._chunk_size <= 0:
            self._chunk_size = 2097152
        self._speed_limit: int = speed_limit if speed_limit is not None else env_speed_limit
        if self._speed_limit < 0:
            self._speed_limit = 0
        self._token: str | None = token if token is not None else getenv("GF_TOKEN")

        self._password: str | None = password
        self._url_or_file: str = url_or_file

        self._session: Session = Session()
        self._stop_event: Event = Event()
        selected_root: str | None = root_dir if root_dir is not None else env_root_dir
        self._root_dir: str = selected_root if selected_root else getcwd()

        self._session.headers.update({
            "Accept-Encoding": "gzip",
            "User-Agent": self._user_agent if self._user_agent else "Mozilla/5.0",
            "Connection": "keep-alive",
            "Accept": "*/*",
            "Origin": "https://gofile.io",
            "Referer": "https://gofile.io/",
        })


    def _parse_url_or_file(self) -> None:
        """
        _parse_url_or_file

        Parses a file or a url for possible links.

        :return:
        """

        if not (path.exists(self._url_or_file) and path.isfile(self._url_or_file)):
            downloader: Downloader = Downloader(
                self._root_dir,
                self._interactive,
                self._max_workers,
                self._number_retries,
                self._timeout,
                self._chunk_size,
                self._stop_event,
                self._session,
                self._url_or_file,
                self._password,
                self._speed_limit,
            )

            downloader.run()

            return

        with open(self._url_or_file, "r") as f:
            lines: list[str] = f.readlines()

        with ThreadPoolExecutor(max_workers=self._batch_threads) as executor:
            for line in lines:
                if self._stop_event.is_set():
                    return

                line_splitted: list[str] = line.split(" ")
                url: str = line_splitted[0].strip()
                password: str | None = self._password if self._password else line_splitted[1].strip() \
                    if len(line_splitted) > 1 else self._password
                downloader: Downloader = Downloader(
                    self._root_dir,
                    False, # Disable interactive download when downloading a batch from text file.
                    self._max_workers,
                    self._number_retries,
                    self._timeout,
                    self._chunk_size,
                    self._stop_event,
                    self._session,
                    url,
                    password,
                    self._speed_limit,
                )

                executor.submit(downloader.run)


    def run(self) -> None:
        """
        run

        This method starts the download process after the creation of the Downloader object.

        :return:
        """

        signal(SIGINT, self._handle_sigint)
        _print_banner()
        _print(f"Starting, please wait...{NEW_LINE}")
        self._set_account_access_token(self._token)
        self._parse_url_or_file()


    @staticmethod
    def _read_int_env(name: str, default: int, allow_zero: bool = False) -> int:
        """
        _read_int_env

        Reads integer values from environment safely.

        :param name: environment variable name.
        :param default: fallback value.
        :param allow_zero: whether zero is accepted.
        :return:
        """

        raw_value: str | None = getenv(name)
        if raw_value is None:
            return default
        try:
            parsed: int = int(raw_value)
        except ValueError:
            return default
        if allow_zero and parsed == 0:
            return 0
        return parsed if parsed > 0 else default


    @staticmethod
    def _read_float_env(name: str, default: float) -> float:
        """
        _read_float_env

        Reads float values from environment safely.

        :param name: environment variable name.
        :param default: fallback value.
        :return:
        """

        raw_value: str | None = getenv(name)
        if raw_value is None:
            return default
        try:
            parsed: float = float(raw_value)
        except ValueError:
            return default
        return parsed if parsed > 0 else default


    def _set_account_access_token(self, token: str | None = None) -> None:
        """
        _set_account_access_token

        Get a new access token for the account created or use the token provided for an already existent account.

        :param token: token to be used accross connections if available.
        :return:
        """

        if token:
            self._session.cookies.set("Cookie", f"accountToken={token}")
            self._session.headers.update({"Authorization": f"Bearer {token}"})
            return

        response: dict[Any, Any] = {}
        
        user_agent: str = str(self._session.headers.get("User-Agent", "Mozilla/5.0"))
        wt: str = generate_website_token(user_agent, "")

        for _ in range(self._number_retries):
            try:
                response = self._session.post(
                    "https://api.gofile.io/accounts",
                    headers={
                        "X-Website-Token": wt,
                        "X-BL": "en-US"
                    },
                    timeout=self._timeout
                ).json()
            except Timeout:
                continue
            else:
                break

        if not response and response["status"] != "ok":
            die("Account creation failed!")

        self._session.cookies.set("Cookie", f"accountToken={response['data']['token']}")
        self._session.headers.update({"Authorization": f"Bearer {response['data']['token']}"})


    def _stop(self) -> None:
        """
        _stop

        Stops all work from continuing.

        :return:
        """

        _print(f"{TERMINAL_CLEAR_LINE}Stopping, please wait...{NEW_LINE}")
        self._stop_event.set()


    def _handle_sigint(self, _: int, __: FrameType | None) -> None:
        """
        _handle_sigint

        Signal handler triggered when a SIGINT (when pressing CTRL-C) is received.
        Issues the stop event so that the running tasks can close gracefully,
        ignoring tasks that didn't start yet.

        :param signum:  Signal number received (for this callback usually SIGINT).
        :param frame:   FrameType object representing the current stack frame
                        where the received signal was caught.
        :return:
        """

        if not self._stop_event.is_set():
            self._stop()
            signal(SIGINT, SIG_IGN)



def _parse_speed_limit(value: str) -> int:
    normalized: str = value.strip().lower()
    multipliers: dict[str, int] = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}

    if not normalized:
        return 0
    if normalized[-1] in multipliers:
        base: float = float(normalized[:-1])
        return int(base * multipliers[normalized[-1]])
    return int(float(normalized))


def _prompt_with_default(prompt: str, default: str) -> str:
    value: str = input(f"{prompt} [{default}]: ").strip()
    return value if value else default


def _prompt_bool(prompt: str, default: bool) -> bool:
    default_hint: str = "Y/n" if default else "y/N"
    value: str = input(f"{prompt} ({default_hint}): ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes", "1", "true")


def _build_parser() -> ArgumentParser:
    parser: ArgumentParser = ArgumentParser(description="Download files from gofile.io")
    parser.add_argument("target", nargs="?", help="Gofile URL or text file path")
    parser.add_argument("password", nargs="?", help="Optional password")
    parser.add_argument("-i", "--interactive", action="store_true", help="Enable file selection prompt")
    parser.add_argument("-d", "--download-dir", help="Directory where files will be saved")
    parser.add_argument("--parallel-downloads", type=int, help="How many files to download in parallel")
    parser.add_argument("--threads", type=int, help="How many batch threads to run")
    parser.add_argument("--speed-limit", help="Per-download limit in B/s (supports k/m/g suffixes)")
    parser.add_argument("--max-retries", type=int, help="Maximum retries on timeout")
    parser.add_argument("--timeout", type=float, help="Connection timeout in seconds")
    parser.add_argument("--chunk-size", type=int, help="Chunk size in bytes")
    parser.add_argument("--user-agent", help="Custom user-agent")
    parser.add_argument("--token", help="Account token")
    return parser


def _collect_interactive_cli_values() -> Namespace:
    target: str = input("Enter gofile URL or text file path: ").strip()
    while not target:
        target = input("Please provide a URL or text file path: ").strip()

    password: str = input("Password (optional): ").strip()
    parallel_downloads: str = _prompt_with_default("Parallel downloads (files)", "5")
    threads: str = _prompt_with_default("Threads total (batch)", parallel_downloads)
    speed_limit: str = _prompt_with_default("Speed limit per download (B/s, 0 = unlimited)", "0")
    interactive: bool = _prompt_bool("Enable file selection prompt", True)

    return Namespace(
        target=target,
        password=password if password else None,
        interactive=interactive,
        download_dir=None,
        parallel_downloads=int(parallel_downloads),
        threads=int(threads),
        speed_limit=speed_limit,
        max_retries=None,
        timeout=None,
        chunk_size=None,
        user_agent=None,
        token=None,
    )


def main() -> None:
    parser: ArgumentParser = _build_parser()
    args: Namespace = parser.parse_args()

    if not args.target:
        args = _collect_interactive_cli_values()

    speed_limit: int | None = None
    if args.speed_limit is not None:
        try:
            speed_limit = _parse_speed_limit(str(args.speed_limit))
        except ValueError:
            die("Invalid speed limit value.")

    manager: Manager = Manager(
        url_or_file=args.target,
        password=args.password,
        interactive=args.interactive,
        max_workers=args.parallel_downloads,
        number_retries=args.max_retries,
        timeout=args.timeout,
        user_agent=args.user_agent,
        chunk_size=args.chunk_size,
        root_dir=args.download_dir,
        speed_limit=speed_limit,
        batch_threads=args.threads,
        token=args.token,
    )
    manager.run()


if __name__ == "__main__":
    main()
