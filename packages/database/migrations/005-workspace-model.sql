SET LOCAL ROLE hub_schema_owner;
CREATE TABLE hub_tenancy.workspace_models (
    tenant_id uuid NOT NULL, workspace_id uuid NOT NULL,
    model text NOT NULL CHECK(length(model) BETWEEN 1 AND 120),
    instructions text NOT NULL DEFAULT '' CHECK(length(instructions)<=4000),
    encrypted_key bytea NOT NULL,
    PRIMARY KEY (tenant_id,workspace_id),
    FOREIGN KEY (tenant_id,workspace_id) REFERENCES hub_tenancy.workspaces
);
RESET ROLE;
ALTER TABLE hub_tenancy.workspace_models ENABLE ROW LEVEL SECURITY;
ALTER TABLE hub_tenancy.workspace_models FORCE ROW LEVEL SECURITY;
CREATE POLICY model_member ON hub_tenancy.workspace_models FOR SELECT TO hub_portal USING
    (EXISTS(SELECT FROM hub_tenancy.workspaces w WHERE (w.tenant_id,w.id)=(workspace_models.tenant_id,workspace_models.workspace_id)));
CREATE POLICY model_manager ON hub_tenancy.workspace_models TO hub_portal USING
    (hub_admin.can_manage_space(tenant_id,workspace_id)) WITH CHECK (hub_admin.can_manage_space(tenant_id,workspace_id));
GRANT SELECT, INSERT, UPDATE(model,instructions,encrypted_key), DELETE ON hub_tenancy.workspace_models TO hub_portal;

ALTER TABLE hub_tenancy.agent_runs ADD COLUMN conversation_key text;
SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.audit_workspace_model() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid := nullif(current_setting('hub.principal_id',true),'')::uuid;
BEGIN
    INSERT INTO hub_tenancy.audit_logs(tenant_id,actor_id,action) VALUES
    (coalesce(NEW.tenant_id,OLD.tenant_id),actor,concat_ws(':','model',TG_OP,coalesce(NEW.workspace_id,OLD.workspace_id)));
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION hub_admin.audit_workspace_model() FROM PUBLIC;
RESET ROLE;
CREATE TRIGGER model_audit AFTER INSERT OR UPDATE OR DELETE ON hub_tenancy.workspace_models
FOR EACH ROW EXECUTE FUNCTION hub_admin.audit_workspace_model();
GRANT UPDATE(conversation_key) ON hub_tenancy.agent_runs TO hub_portal;
