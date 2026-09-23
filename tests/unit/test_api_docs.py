def test_swagger_ui_and_redoc_are_available(client) -> None:
    swagger_response = client.get("/docs")
    redoc_response = client.get("/redoc")

    assert swagger_response.status_code == 200
    assert "Swagger UI" in swagger_response.text
    assert "/openapi.json" in swagger_response.text
    assert redoc_response.status_code == 200
    assert "ReDoc" in redoc_response.text


def test_openapi_schema_exposes_professional_api_metadata(client) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "thinkback Memory Service API"
    assert schema["info"]["summary"] == "Thinkback 记忆服务接口文档"
    assert "首版主链路记忆服务" in schema["info"]["description"]
    assert "Swagger UI" in schema["info"]["description"]
    assert "真实质量、性能与稳定性结论应以脚本输出和发布归档为准" in schema["info"]["description"]
    assert "质量评测方案" not in schema["info"]["description"]
    assert "评测报告模板" not in schema["info"]["description"]
    assert schema["info"]["version"] == "0.1.0"
    assert schema["servers"] == [
        {"url": "http://localhost:8000", "description": "本地调试"},
        {"url": "/", "description": "当前部署环境"},
    ]
    assert {tag["name"]: tag["description"] for tag in schema["tags"]} == {
        "health": "服务健康、存活和依赖就绪检查。",
        "memory": "记忆写入、召回、管理、删除和后台任务查询。",
    }


def test_openapi_schema_does_not_expose_internal_scope_fields(client) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    # 业务 API 不暴露内部作用域字段；/admin/api 治理面是内部接口，合法暴露，
    # 从路径与组件两个维度排除后再断言。
    document = response.json()
    business_paths = {
        path: value
        for path, value in document["paths"].items()
        if not path.startswith("/admin")
    }
    business_schemas = {
        name: component
        for name, component in document.get("components", {}).get("schemas", {}).items()
        if not name.startswith("Admin")
    }
    business_view = f"{business_paths}{business_schemas}"
    assert "character_id" not in business_view
    assert "memory_scope_id" not in business_view


def test_openapi_schema_documents_memory_workflow_operations(client) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/memory/append"]["post"]["summary"] == "写入一轮对话记忆"
    assert schema["paths"]["/memory/append"]["post"]["operationId"] == "memory_append"
    assert "完整 user -> assistant 轮次" in schema["paths"]["/memory/append"]["post"]["description"]
    assert schema["paths"]["/memory/recall"]["post"]["summary"] == "召回用户相关记忆"
    assert schema["paths"]["/memory/recall"]["post"]["operationId"] == "memory_recall"
    assert "L1/L2/L3" in schema["paths"]["/memory/recall"]["post"]["description"]
    assert schema["paths"]["/memory/delete"]["post"]["summary"] == "删除记忆或记忆范围"
    assert schema["paths"]["/memory/delete"]["post"]["operationId"] == "memory_delete"
    assert schema["paths"]["/memory/items"]["get"]["summary"] == "列出可管理的长期记忆"
    assert schema["paths"]["/memory/items"]["get"]["operationId"] == "memory_list_items"
    assert schema["paths"]["/memory/items/{memory_id}"]["get"]["summary"] == "查询单条可管理记忆"
    assert schema["paths"]["/memory/items/{memory_id}"]["get"]["operationId"] == "memory_get_item"
    assert schema["paths"]["/memory/update"]["post"]["summary"] == "编辑单条长期记忆"
    assert schema["paths"]["/memory/update"]["post"]["operationId"] == "memory_update_item"
    assert "/memory/rebuild" not in schema["paths"]
    assert schema["paths"]["/memory/tasks/{task_id}"]["get"]["summary"] == "查询记忆后台任务"
    assert schema["paths"]["/memory/tasks/{task_id}"]["get"]["operationId"] == "memory_get_task"
    assert (
        schema["paths"]["/memory/l3/background-status"]["get"]["summary"] == "查看 L3 后台队列状态"
    )
    assert (
        schema["paths"]["/memory/l3/background-status"]["get"]["operationId"]
        == "memory_l3_background_status"
    )


def test_openapi_schema_documents_memory_request_and_response_fields(client) -> None:
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]

    schema_names = [
        "MemoryMessage",
        "AppendMemoryRequest",
        "RecallMemoryRequest",
        "DeleteMemoryRequest",
        "UpdateMemoryRequest",
        "MemoryItem",
        "ManagedMemoryItem",
        "AppendMemoryResponse",
        "RecallMemoryResponse",
        "DeleteMemoryResponse",
        "UpdateMemoryResponse",
        "ListMemoriesResponse",
        "GetMemoryResponse",
        "TaskResponse",
        "L3BackgroundStatusResponse",
    ]
    for schema_name in schema_names:
        properties = components[schema_name]["properties"]
        assert properties, schema_name
        for field_name, field_schema in properties.items():
            assert field_schema.get("description"), f"{schema_name}.{field_name}"

    task_id_param = schema["paths"]["/memory/tasks/{task_id}"]["get"]["parameters"][0]
    assert task_id_param["name"] == "task_id"
    assert task_id_param["description"] == "要查询的记忆后台任务 ID。"
    assert (
        "completed、running 或 already_done"
        in components["DeleteMemoryResponse"]["properties"]["status"]["description"]
    )
    assert "memory_scope_id" not in str(components["ManagedMemoryItem"])
    assert "backend_memory_id" not in str(components["ManagedMemoryItem"])
    assert "source_refs" not in str(components["ManagedMemoryItem"])

    l3_status_response = schema["paths"]["/memory/l3/background-status"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert l3_status_response["$ref"] == "#/components/schemas/L3BackgroundStatusResponse"


def test_openapi_schema_documents_health_operations(client) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/health"]["get"]["summary"] == "基础健康检查"
    assert schema["paths"]["/health"]["get"]["operationId"] == "health_check"
    assert schema["paths"]["/health/live"]["get"]["summary"] == "存活检查"
    assert schema["paths"]["/health/live"]["get"]["operationId"] == "health_live"
    assert schema["paths"]["/health/ready"]["get"]["summary"] == "依赖就绪检查"
    assert schema["paths"]["/health/ready"]["get"]["operationId"] == "health_ready"
