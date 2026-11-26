SET LOCAL ROLE hub_provisioning_code;

CREATE FUNCTION hub_admin.iaves_customer_signups(since timestamptz)
RETURNS TABLE (account_id bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    PERFORM hub_admin.require_platform_owner();
    RETURN QUERY
        SELECT DISTINCT i.subject::bigint
        FROM hub_tenancy.audit_logs a
        JOIN hub_identity.identities i ON i.user_id=a.actor_id
        WHERE a.tenant_id='e7eb1e21-00c7-4abd-8ada-641ce20a91b3'
          AND a.action LIKE 'client.workspace_enrolled:%' AND a.created_at >= since
          AND i.issuer='urn:hubia:portal:local' AND i.subject ~ '^[1-9][0-9]{0,17}$';
END $$;

REVOKE ALL ON FUNCTION hub_admin.iaves_customer_signups(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.iaves_customer_signups(timestamptz) TO hub_portal;
RESET ROLE;
