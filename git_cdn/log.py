# Standard Library
import logging
import numbers
import socket
import sys
import uuid
from logging.handlers import DatagramHandler
from time import sleep

# Third Party Libraries
import structlog
import ujson
from structlog.threadlocal import bind_threadlocal
from structlog.threadlocal import clear_threadlocal

g_version = "unknown"
g_host = socket.gethostname()


# Move all event_dict fields into extra, and rename event to message for vector.dev
def extra_field(logger, method_name, event_dict):
    if "event" not in event_dict:
        return event_dict
    message = event_dict.pop("event")
    newdict = {}
    newdict["message"] = message
    if event_dict:
        newdict["extra"] = event_dict
    return newdict


# Move all event_dict fields into extra, and rename event to message for vector.dev
def un_extra_field(logger, method_name, event_dict):
    if "extra" in event_dict:
        event_dict.update(event_dict.pop("extra"))
    if "message" in event_dict:
        event_dict["event"] = event_dict.pop("message")
    return event_dict


gunicorn_access = [
    "first_request_line",
    "remote_address",
    "request_header",
    "request_time_micro",
    "response_size",
    "response_status",
]


class HostUnreachable(Exception):
    pass


def wait_host_resolve(host):
    for _ in range(0, 120):
        try:
            if socket.gethostbyname(host):
                return
        except socket.gaierror:
            sleep(1)
            # logger is not ready yet, so use print
            print("logging host {} not found, retrying".format(host))
    raise HostUnreachable("logging host {} not found".format(host))


class UdpJsonHandler(DatagramHandler):
    @staticmethod
    def basedict(record):
        return {
            "application_name": "git-cdn",
            "application_version": g_version,
            "facility": record.name,
            "function": record.funcName,
            "host": g_host,
            "levelname": record.levelname.lower(),
            "line": record.lineno,
            "pid": record.process,
        }

    def makePickle(self, record):
        msg_dict = self.basedict(record)
        msg_dict = structlog.contextvars.merge_contextvars(None, None, msg_dict)
        msg_dict = structlog.threadlocal.merge_threadlocal(None, None, msg_dict)
        msg_dict = structlog.processors.TimeStamper(fmt="iso")(None, None, msg_dict)
        if isinstance(record.msg, dict):
            msg_dict.update(record.msg)
        else:
            msg_dict["message"] = record.getMessage()
            extra = {
                k: getattr(record, k) for k in gunicorn_access if hasattr(record, k)
            }
            if extra:
                msg_dict["extra"] = extra

        json_msg = (
            ujson.dumps(msg_dict, escape_forward_slashes=False, reject_bytes=False)
            + "\n"
        )
        # Truncate message if too big, UDP has a limit at 64k,
        # take some margin for protocol/wrappers
        if len(json_msg) > 60000:
            trunc_dict = self.basedict(record)
            trunc_dict["message"] = msg_dict["message"][:10000]
            trunc_dict["truncated"] = True
            json_msg = (
                ujson.dumps(
                    trunc_dict, escape_forward_slashes=False, reject_bytes=False
                )
                + "\n"
            )
        return json_msg.encode()


shared_processors = [
    structlog.stdlib.filter_by_level,
    structlog.stdlib.PositionalArgumentsFormatter(),
    structlog.processors.UnicodeDecoder(),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    extra_field,
    structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
]


def configure_log():
    logging.getLogger().setLevel(logging.DEBUG)
    structlog.configure(
        processors=shared_processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def enable_udp_logs(host="127.0.0.1", port=3465, version=None):
    if version:
        global g_version
        g_version = version

    rlog = logging.getLogger()
    configure_log()

    # wait for host dns to be reachable to avoid dropping first logs
    wait_host_resolve(host)

    udpJsonHandler = UdpJsonHandler(host=host, port=port)
    rlog.addHandler(udpJsonHandler)

    # Add uuid for thread
    clear_threadlocal()
    bind_threadlocal(uuid=str(uuid.uuid4()))


def enable_console_logs(level=None, output=sys.stdout):
    configure_log()
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M.%S"),
            un_extra_field,
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ],
    )

    stderr_level = logging.ERROR
    err_handler = logging.StreamHandler(sys.stderr)
    err_handler.setLevel(stderr_level)
    err_handler.setFormatter(formatter)

    out_handler = logging.StreamHandler(output)
    if level:
        out_handler.setLevel(level)
    out_handler.setFormatter(formatter)

    class ignoreError(logging.Filter):
        def filter(self, record):
            return record.levelno < stderr_level

    out_handler.addFilter(ignoreError())

    rlog = logging.getLogger()
    rlog.addHandler(out_handler)
    rlog.addHandler(err_handler)
