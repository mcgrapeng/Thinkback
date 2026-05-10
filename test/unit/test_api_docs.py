def test_swagger_ui_and_redoc_are_available(client) -> None:
    swagger_response = client.get("/docs")
    redoc_response = client.get("/redoc")

    assert swagger_response.status_code == 200
    assert "Swagger UI" in swagger_response.text
    assert redoc_response.status_code == 200
    assert "ReDoc" in redoc_response.text


def test_openapi_schema_exposes_professional_api_metadata(client) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Thinkback Memory Service API"
    assert "首版主链路记忆服务" in schema["info"]["description"]
    assert "Swagger UI" in schema["info"]["description"]
    assert schema["info"]["version"] == "0.1.0"
    assert {tag["name"]: tag["description"] for tag in schema["tags"]} == {
        "health": "服务健康、存活和依赖就绪检查。",
        "memory": "记忆写入、召回、删除、重建和后台任务查询。",
    }


def test_openapi_schema_documents_memory_workflow_operations(client) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/memory/append"]["post"]["summary"] == "写入一轮对话记忆"
    assert "完整 user -> assistant 轮次" in schema["paths"]["/memory/append"]["post"]["description"]
    assert schema["paths"]["/memory/recall"]["post"]["summary"] == "召回用户相关记忆"
    assert "L1/L2/L3" in schema["paths"]["/memory/recall"]["post"]["description"]
    assert schema["paths"]["/memory/delete"]["post"]["summary"] == "删除记忆或记忆范围"
    assert schema["paths"]["/memory/rebuild"]["post"]["summary"] == "重建记忆摘要和长期记忆"
    assert schema["paths"]["/memory/tasks/{task_id}"]["get"]["summary"] == "查询记忆后台任务"
    assert schema["paths"]["/memory/l3/background-status"]["get"]["summary"] == "查看 L3 后台队列状态"


def test_openapi_schema_documents_health_operations(client) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["paths"]["/health"]["get"]["summary"] == "基础健康检查"
    assert schema["paths"]["/health/live"]["get"]["summary"] == "存活检查"
    assert schema["paths"]["/health/ready"]["get"]["summary"] == "依赖就绪检查"
