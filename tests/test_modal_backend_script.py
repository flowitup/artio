"""Structural checks on the Modal backend: its method surface, the ComfyUI probe and the unchanged graph."""

import ast

import pytest

WEB_DECORATORS = {"modal.fastapi_endpoint", "modal.web_endpoint", "modal.asgi_app", "modal.wsgi_app", "modal.web_server"}


def _backend_class(source: str) -> ast.ClassDef:
    tree = ast.parse(source)
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Qwen21UC")


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}


def _decorator_names(func: ast.FunctionDef) -> set[str]:
    names = set()
    for decorator in func.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            names.add(f"{target.value.id}.{target.attr}")
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


@pytest.mark.parametrize("name", ["ping", "generate", "run_workflow"])
def test_backend_class_exposes_ping_generate_and_run_workflow(backend_source, name):
    methods = _methods(_backend_class(backend_source))
    assert name in methods
    assert "modal.method" in _decorator_names(methods[name])


def test_backend_serves_no_web_endpoint(backend_source):
    for func in _methods(_backend_class(backend_source)).values():
        assert not _decorator_names(func) & WEB_DECORATORS, f"{func.name} is still a web endpoint"
    assert "fastapi" not in backend_source


def test_ping_probes_comfyui_system_stats(backend_source):
    ping = _methods(_backend_class(backend_source))["ping"]
    gets = [
        node
        for node in ast.walk(ping)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "requests"
    ]
    assert gets, "ping must call requests.get"
    url = gets[0].args[0]
    assert isinstance(url, ast.JoinedStr)
    tail = url.values[-1]
    assert isinstance(tail, ast.Constant) and str(tail.value).endswith("/system_stats")
    assert any(isinstance(node, ast.Raise) for node in ast.walk(ping))


def test_build_workflow_still_builds_the_eight_node_graph(backend_script):
    graph = backend_script.build_workflow("p", 1088, 1920, 25, 7)
    assert sorted(graph, key=int) == [str(i) for i in range(1, 9)]
    sampler = graph["6"]
    assert sampler["class_type"] == "KSampler"
    assert sampler["inputs"]["seed"] == 7
