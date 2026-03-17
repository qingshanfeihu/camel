import collections
import os
import subprocess
import threading
import time
import asyncio
from typing import Optional, Deque

from camel.logger import get_logger

logger = get_logger(__name__)

class LocalMinerUReader:
    r"""Local MinerU Reader for processing documents and returning content
    in structured formats, mimicking ChunkrReader's interface but running locally.
    
    Args:
        output_dir (Optional[str], optional): Directory to store output files.
            If not provided, defaults to 'mineru_output' in current working directory.
    """

    def __init__(
        self,
        output_dir: Optional[str] = None,
        mineru_command: str = "mineru",
    ) -> None:
        self.output_dir = output_dir or os.path.join(os.getcwd(), "mineru_output")
        self.mineru_command = mineru_command
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)

    async def submit_task(
        self,
        file_path: str,
        extra_args: Optional[list] = None,
        cwd: Optional[str] = None,
    ) -> str:
        r"""Submits a file to the local MinerU CLI and returns the task ID (filename).

        Args:
            file_path (str): The path to the file to be processed.
            extra_args (Optional[list]): Additional command line arguments for mineru.
            cwd (Optional[str]): The working directory for the subprocess.

        Returns:
            str: The task ID (which is the input filename without extension).
        """
        return await asyncio.to_thread(self._submit_task_sync, file_path, extra_args, cwd)

    def _submit_task_sync(
        self,
        file_path: str,
        extra_args: Optional[list] = None,
        cwd: Optional[str] = None,
    ) -> str:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_name = os.path.basename(file_path)
        task_id = os.path.splitext(file_name)[0]
        
        # Construct command
        # mineru <pdf_path> --output-dir <output_dir>
        
        cmd = [
            self.mineru_command,
            "-p", file_path,
            "-o", self.output_dir,
        ]

        if extra_args:
            cmd.extend(extra_args)
        
        logger.info(f"Running MinerU command: {' '.join(cmd)}")
        
        # Prepare environment with debug logging
        env = os.environ.copy()
        env["LOGURU_LEVEL"] = "DEBUG"
        
        try:
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            last_output_time = time.monotonic()
            output_lock = threading.Lock()
            # Keep last 20 lines of output for debugging hangs
            recent_logs: Deque[str] = collections.deque(maxlen=20)

            def _touch_output_time() -> None:
                nonlocal last_output_time
                with output_lock:
                    last_output_time = time.monotonic()

            def _stream_output(stream, log_fn):
                if stream is None:
                    return
                for line in stream:
                    line = line.rstrip()
                    if line:
                        log_fn(line)
                        with output_lock:
                            recent_logs.append(line)
                        _touch_output_time()

            def _watch_output():
                while process.poll() is None:
                    with output_lock:
                        idle_seconds = time.monotonic() - last_output_time
                        current_logs = list(recent_logs)
                    
                    if idle_seconds >= 60:
                        logger.warning(
                            "MinerU still running (no output for %.0f s)...",
                            idle_seconds,
                        )
                        if current_logs:
                            logger.warning("Last %d lines of output:", len(current_logs))
                            for log_line in current_logs:
                                logger.warning("  > %s", log_line)
                        else:
                            logger.warning("  (No output received yet)")
                            
                        _touch_output_time()
                    time.sleep(5)

            stdout_thread = None
            stderr_thread = None
            if process.stdout is not None:
                stdout_thread = threading.Thread(
                    target=_stream_output,
                    args=(process.stdout, logger.info),
                    daemon=True,
                )
                stdout_thread.start()

            if process.stderr is not None:
                stderr_thread = threading.Thread(
                    target=_stream_output,
                    args=(process.stderr, logger.warning),
                    daemon=True,
                )
                stderr_thread.start()

            watchdog_thread = threading.Thread(
                target=_watch_output,
                daemon=True,
            )
            watchdog_thread.start()

            return_code = process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()

            if return_code != 0:
                logger.error(f"MinerU failed with return code {return_code}")
                raise RuntimeError(
                    f"MinerU processing failed with return code {return_code}"
                )

            logger.info(f"MinerU processing completed for {file_name}")
            return task_id
        except Exception as e:
            logger.error("MinerU processing failed: %s", e)
            raise

    async def get_task_output(self, task_id: str) -> str:
        r"""Retrieves the processed output for a given task ID.

        Args:
            task_id (str): The task ID returned by submit_task.

        Returns:
            str: The raw JSON content of the processed file.
        """
        # The output file is expected to be at:
        # output_dir/task_id/task_id_content_list.json
        # OR inside a backend subfolder (e.g. hybrid_auto)
        task_dir = os.path.join(self.output_dir, task_id)
        if not os.path.exists(task_dir):
            raise FileNotFoundError(f"Task directory not found: {task_dir}")
             
        # Search for content_list.json recursively
        target_file = None
        for root, dirs, files in os.walk(task_dir):
            for file in files:
                if file.endswith("_content_list.json"):
                    target_file = os.path.join(root, file)
                    break
            if target_file:
                break

        if not target_file:
            raise FileNotFoundError(f"Content list JSON not found in {task_dir}")

        with open(target_file, "r", encoding="utf-8") as f:
            return f.read()
