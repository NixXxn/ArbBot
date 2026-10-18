"""Run a foreground child with a rotating, unbuffered console log; preserve its exit code."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import subprocess
import sys


def main():
    log = Path(sys.argv[1])
    command = sys.argv[2:]
    if not command:raise SystemExit('Missing command')
    log.parent.mkdir(parents=True,exist_ok=True)
    handler = RotatingFileHandler(log,maxBytes=2*1024*1024,backupCount=2,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger = logging.getLogger('bot-console');logger.setLevel(logging.INFO);logger.addHandler(handler)
    process = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                               text=True,bufsize=1)
    try:
        for line in process.stdout:logger.info(line.rstrip('\n'))
        return process.wait()
    except KeyboardInterrupt:
        # The terminal sends SIGINT to the foreground group, including the child.
        # Wait for the live runner to persist interruption before exiting.
        try:return process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.terminate()
            return process.wait()
    finally:
        handler.close()


if __name__=='__main__':raise SystemExit(main())
