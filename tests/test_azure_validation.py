from traceforge.azure_validation import validate_deployment_outputs


def _outputs(**values):
    return {key: {"type": "String", "value": value} for key, value in values.items()}


def test_azure_output_validation_accepts_hardened_profile():
    outputs = _outputs(
        gatewayOtlpEndpoint="gateway.example:443",
        privateEndpointsEnabled=True,
        gatewayMutualTlsEnabled=True,
        gatewayTenantAuthEnabled=True,
        viewerUrl="https://viewer.example",
        viewerCallbackUrl="https://viewer.example/.auth/login/aad/callback",
    )

    assert validate_deployment_outputs(
        outputs,
        require_private=True,
        require_mtls=True,
        require_tenant_auth=True,
        require_viewer=True,
    ) == []


def test_azure_output_validation_reports_missing_controls():
    outputs = _outputs(
        gatewayOtlpEndpoint="gateway.example:443",
        privateEndpointsEnabled=False,
        gatewayMutualTlsEnabled=False,
        gatewayTenantAuthEnabled=False,
        viewerUrl="",
        viewerCallbackUrl="",
    )

    failures = validate_deployment_outputs(
        outputs,
        require_private=True,
        require_mtls=True,
        require_tenant_auth=True,
        require_viewer=True,
    )

    assert "privateEndpointsEnabled is not enabled" in failures
    assert "gatewayMutualTlsEnabled is not enabled" in failures
    assert "gatewayTenantAuthEnabled is not enabled" in failures
    assert "viewerUrl is missing" in failures
    assert "viewerCallbackUrl is missing or invalid" in failures


def test_azure_output_validation_requires_gateway_endpoint():
    assert validate_deployment_outputs({}) == ["gatewayOtlpEndpoint is missing"]
