"""Wire codecs for the narrow native A2A 1.0 JSON-RPC profile."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_commons.a2a_tasks import RPCRequest

METHODS = {
    "SendMessage": "message/send",
    "GetTask": "tasks/get",
    "CancelTask": "tasks/cancel",
    "ListTasks": "tasks/list",
}
WIRE_METHODS = {method: wire for wire, method in METHODS.items()}
STATE_PREFIX = "TASK_STATE_"


def _integer(value: object, minimum: int, maximum: int) -> object:
    # Decimal was parsed directly from the wire. Check the small schema bounds
    # before converting so huge exponents cannot allocate an enormous integer.
    if isinstance(value, Decimal) and minimum <= value <= maximum:
        if value == value.to_integral_value():
            return int(value)
    return value


def _summary(value: dict) -> dict:
    bounds = {
        "count": (0, 128),
        "total": (-128_000_000, 128_000_000),
        "minimum": (-(10**6), 10**6),
        "maximum": (-(10**6), 10**6),
    }
    return {k: _integer(v, *bounds[k]) if k in bounds else v for k, v in value.items()}


def _input(data: dict) -> dict:
    value = data.get("input")
    if not isinstance(value, dict):
        return data
    skill = data.get("skill")
    if skill == "analysis.summary@1" and isinstance(values := value.get("values"), list):
        return {
            **data,
            "input": {**value, "values": [_integer(x, -(10**6), 10**6) for x in values]},
        }
    if skill == "report.markdown@1" and isinstance(summary := value.get("summary"), dict):
        return {**data, "input": {**value, "summary": _summary(summary)}}
    return data


def decode_request(rpc: RPCRequest) -> RPCRequest:
    from agent_commons.a2a_tasks import RPCError

    method = METHODS.get(rpc.method)
    if method is None:
        raise RPCError(-32601, "Method not found")
    params = rpc.params
    if not isinstance(params, dict):
        raise RPCError(-32602, "Expected object parameters")
    params = dict(params)
    if method == "message/send":
        msg = params.get("message")
        if (
            not isinstance(msg, dict)
            or set(msg) != {"messageId", "role", "parts"}
            or msg["role"] != "ROLE_USER"
            or not isinstance(msg["parts"], list)
            or len(msg["parts"]) != 1
            or not isinstance(msg["parts"][0], dict)
            or set(msg["parts"][0]) != {"data"}
            or not isinstance(msg["parts"][0]["data"], dict)
        ):
            raise RPCError(-32602, "Expected one structured user message")
        params["message"] = {
            **msg,
            "kind": "message",
            "role": "user",
            "parts": [{"kind": "data", "data": _input(msg["parts"][0]["data"])}],
        }
        if "configuration" in params:
            config = params["configuration"]
            if (
                not isinstance(config, dict)
                or set(config) - {"returnImmediately", "acceptedOutputModes", "historyLength"}
                or ("returnImmediately" in config and config["returnImmediately"] is not True)
            ):
                raise RPCError(-32602, "Only immediate typed execution is supported")
            params["configuration"] = {k: v for k, v in config.items() if k != "returnImmediately"}
    elif method == "tasks/list" and isinstance(state := params.get("status"), str):
        if not state.startswith(STATE_PREFIX):
            raise RPCError(-32602, "Invalid task status")
        params["status"] = state.removeprefix(STATE_PREFIX).lower()
    try:
        json.dumps(params, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise RPCError(-32602, "Unsupported numeric representation") from None
    return rpc.model_copy(update={"method": method, "params": params})


def encode_request(method: str, params: dict) -> dict:
    if method == "message/send":
        msg = params["message"]
        return {
            "message": {
                "messageId": msg["messageId"],
                "role": "ROLE_USER",
                "parts": [{"data": part["data"]} for part in msg["parts"]],
            },
            "configuration": {"returnImmediately": True},
        }
    return params


def _wire_task(task: dict) -> dict:
    result = {k: v for k, v in task.items() if k != "kind"}
    state = task["status"]["state"]
    result["status"] = {**task["status"], "state": STATE_PREFIX + state.upper()}
    if "artifacts" in task:
        result["artifacts"] = [
            {**artifact, "parts": [{"data": p["data"]} for p in artifact["parts"]]}
            for artifact in task["artifacts"]
        ]
    return result


def encode_result(method: str, result: dict) -> dict:
    if method == "tasks/list":
        return {**result, "tasks": [_wire_task(task) for task in result["tasks"]]}
    task = _wire_task(result)
    return {"task": task} if method == "message/send" else task


def decode_result(method: str, result: dict, *, skill: str) -> dict:
    if method == "message/send":
        if not isinstance(result, dict) or set(result) != {"task"}:
            raise ValueError("Expected native task response wrapper")
        task = result["task"]
    else:
        task = result
    if not isinstance(task, dict) or set(task) - {"id", "contextId", "status", "artifacts"}:
        raise ValueError("Unsupported native task response")
    status = task["status"]
    state = status["state"]
    if not isinstance(state, str) or not state.startswith(STATE_PREFIX):
        raise ValueError("Invalid native task status")
    result = {
        **task,
        "kind": "task",
        "status": {**status, "state": state.removeprefix(STATE_PREFIX).lower()},
    }
    if "artifacts" in task:
        artifacts = []
        for artifact in task["artifacts"]:
            parts = []
            for part in artifact["parts"]:
                if not isinstance(part, dict) or set(part) != {"data"}:
                    raise ValueError("Invalid native artifact part")
                data = part["data"]
                if skill == "analysis.summary@1" and isinstance(data, dict):
                    data = _summary(data)
                parts.append({"kind": "data", "data": data})
            artifacts.append({**artifact, "parts": parts})
        result["artifacts"] = artifacts
    return result
