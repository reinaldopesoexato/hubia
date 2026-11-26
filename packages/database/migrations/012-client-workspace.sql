ALTER TABLE hub_tenancy.workspaces ADD COLUMN client_onboarding boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX one_client_onboarding_workspace ON hub_tenancy.workspaces (tenant_id)
    WHERE client_onboarding;
GRANT SELECT (client_onboarding) ON hub_tenancy.workspaces TO hub_provisioning_code;

SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.enroll_client_workspace() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    organization constant uuid := 'e7eb1e21-00c7-4abd-8ada-641ce20a91b3';
    client_space uuid;
BEGIN
    SELECT w.id INTO client_space FROM hub_tenancy.workspaces w
        JOIN hub_tenancy.tenants t ON t.id=w.tenant_id
        WHERE w.tenant_id=organization AND w.client_onboarding AND w.deleted_at IS NULL AND t.status='ACTIVE';
    IF FOUND THEN
        INSERT INTO hub_tenancy.memberships(tenant_id,user_id,role)
            VALUES(organization,NEW.owner_id,'viewer');
        INSERT INTO hub_tenancy.workspace_members(tenant_id,workspace_id,user_id)
            VALUES(organization,client_space,NEW.owner_id);
        INSERT INTO hub_tenancy.audit_logs(tenant_id,actor_id,action)
            VALUES(organization,NEW.owner_id,'client.workspace_enrolled:' || client_space::text);
    END IF;
    RETURN NEW;
END $$;
RESET ROLE;
CREATE TRIGGER enroll_new_client AFTER INSERT ON hub_tenancy.personal_spaces
    FOR EACH ROW EXECUTE FUNCTION hub_admin.enroll_client_workspace();
REVOKE ALL ON FUNCTION hub_admin.enroll_client_workspace() FROM PUBLIC;
