-- Keep agent IDs, run history and workspace-specific credentials when moving ownership.
ALTER TABLE hub_tenancy.agents ALTER COLUMN workspace_id DROP NOT NULL,
    ADD COLUMN retired boolean NOT NULL DEFAULT false,
    ADD UNIQUE (tenant_id,id),
    ADD FOREIGN KEY (tenant_id) REFERENCES hub_tenancy.tenants(id),
    DROP CONSTRAINT agents_kind_check,
    ADD CHECK (kind IN ('guide','knowledge','external','connector','notifications','avihub','library'));
ALTER TABLE hub_tenancy.agent_runs DROP CONSTRAINT agent_runs_tenant_id_workspace_id_agent_id_fkey,
    ADD FOREIGN KEY (tenant_id,workspace_id) REFERENCES hub_tenancy.workspaces,
    ADD FOREIGN KEY (tenant_id,agent_id) REFERENCES hub_tenancy.agents(tenant_id,id);

SET LOCAL ROLE hub_schema_owner;
CREATE TABLE hub_tenancy.agent_spaces (
    tenant_id uuid NOT NULL REFERENCES hub_tenancy.institutions(tenant_id),
    workspace_id uuid NOT NULL, agent_id uuid NOT NULL,
    PRIMARY KEY(tenant_id,workspace_id,agent_id),
    FOREIGN KEY(tenant_id,workspace_id) REFERENCES hub_tenancy.workspaces,
    FOREIGN KEY(tenant_id,agent_id) REFERENCES hub_tenancy.agents(tenant_id,id)
);
CREATE INDEX agent_spaces_by_agent ON hub_tenancy.agent_spaces(tenant_id,agent_id);
RESET ROLE;
INSERT INTO hub_tenancy.agent_spaces
    SELECT a.tenant_id,a.workspace_id,a.id FROM hub_tenancy.agents a
    JOIN hub_tenancy.institutions i USING (tenant_id);
UPDATE hub_tenancy.agents a SET workspace_id=NULL FROM hub_tenancy.institutions i WHERE i.tenant_id=a.tenant_id;

-- On upgrades these connector tables already exist. On a fresh install the connector
-- registers its catalog entry when provisioned, after Django migrations.
DO $$
BEGIN
    IF to_regclass('hub_accounts.bridge_avihubagent') IS NOT NULL THEN
        INSERT INTO hub_tenancy.agents(id,tenant_id,name,kind,description)
        SELECT a.id,a.tenant,'AviHUB','avihub','Consulta equipamentos, pesagens, ração, silos e sensores da unidade autorizada pela sua conta AviHUB.'
        FROM hub_accounts.bridge_avihubagent a JOIN hub_tenancy.institutions i ON i.tenant_id=a.tenant;
    END IF;
    IF to_regclass('hub_accounts.bridge_notelibrary') IS NOT NULL THEN
        INSERT INTO hub_tenancy.agents(id,tenant_id,name,kind,description)
        SELECT l.id,l.connection_id,l.name,'library','Busca e lê notas Markdown nas pastas liberadas para este espaço.'
        FROM hub_accounts.bridge_notelibrary l JOIN hub_tenancy.institutions i ON i.tenant_id=l.connection_id;
    END IF;
END $$;
INSERT INTO hub_tenancy.agent_spaces SELECT w.tenant_id,w.id,a.id
    FROM hub_tenancy.workspaces w JOIN hub_tenancy.agents a USING(tenant_id) WHERE a.kind IN ('library','avihub');

SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.can_manage_catalog(t uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
    SELECT EXISTS (SELECT FROM hub_tenancy.institutions i JOIN hub_tenancy.tenants x ON i.tenant_id=x.id
        JOIN hub_identity.users u ON u.id=nullif(current_setting('hub.principal_id',true),'')::uuid AND u.active
        WHERE i.tenant_id=t AND x.status='ACTIVE' AND (u.platform_owner OR EXISTS (
            SELECT FROM hub_tenancy.memberships m WHERE m.tenant_id=t AND m.user_id=u.id AND m.active
            AND m.role IN ('institution_owner','institution_admin'))))
$$;
REVOKE ALL ON FUNCTION hub_admin.can_manage_catalog(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.can_manage_catalog(uuid) TO hub_portal;
CREATE OR REPLACE FUNCTION hub_admin.seed_workspace_agents() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF EXISTS (SELECT FROM hub_tenancy.personal_spaces WHERE tenant_id=NEW.tenant_id) THEN
        INSERT INTO hub_tenancy.agents(tenant_id,workspace_id,name,kind,description)
        VALUES(NEW.tenant_id,NEW.id,'Guia do espaço','guide','Consulte os agentes disponíveis e as permissões da sua conta neste espaço.');
    END IF;
    RETURN NEW;
END $$;
RESET ROLE;

ALTER TABLE hub_tenancy.agent_spaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE hub_tenancy.agent_spaces FORCE ROW LEVEL SECURITY;
CREATE POLICY members_read ON hub_tenancy.agent_spaces FOR SELECT TO hub_portal USING
    (EXISTS(SELECT FROM hub_tenancy.workspaces w WHERE (w.tenant_id,w.id)=(agent_spaces.tenant_id,agent_spaces.workspace_id)));
CREATE POLICY manage ON hub_tenancy.agent_spaces TO hub_portal USING
    (hub_admin.can_manage_catalog(tenant_id) OR hub_admin.can_manage_space(tenant_id,workspace_id))
    WITH CHECK (hub_admin.can_manage_space(tenant_id,workspace_id));
DROP POLICY members_read ON hub_tenancy.agents;
DROP POLICY manage ON hub_tenancy.agents;
CREATE POLICY members_read ON hub_tenancy.agents FOR SELECT TO hub_portal USING (active AND NOT retired AND (
    EXISTS (SELECT FROM hub_tenancy.workspaces w WHERE (w.tenant_id,w.id)=(agents.tenant_id,agents.workspace_id))
    OR EXISTS (SELECT FROM hub_tenancy.agent_spaces s WHERE (s.tenant_id,s.agent_id)=(agents.tenant_id,agents.id))));
CREATE POLICY manage ON hub_tenancy.agents TO hub_portal USING (
    CASE WHEN workspace_id IS NULL THEN hub_admin.can_manage_catalog(tenant_id)
         ELSE hub_admin.can_manage_space(tenant_id,workspace_id) END)
    WITH CHECK (CASE WHEN workspace_id IS NULL THEN hub_admin.can_manage_catalog(tenant_id)
         ELSE EXISTS (SELECT FROM hub_tenancy.personal_spaces p WHERE p.tenant_id=agents.tenant_id)
              AND hub_admin.can_manage_space(tenant_id,workspace_id) END);
GRANT UPDATE(retired) ON hub_tenancy.agents TO hub_portal;
GRANT SELECT,INSERT,DELETE ON hub_tenancy.agent_spaces TO hub_portal;

SET LOCAL ROLE hub_schema_owner;
CREATE VIEW hub_tenancy.workspace_agents WITH (security_invoker=true) AS
    SELECT a.tenant_id,a.workspace_id,a.id,a.name,a.kind,a.description,a.configuration,a.active
    FROM hub_tenancy.agents a WHERE a.workspace_id IS NOT NULL AND NOT a.retired
    UNION ALL
    SELECT a.tenant_id,s.workspace_id,a.id,a.name,a.kind,a.description,a.configuration,a.active
    FROM hub_tenancy.agents a JOIN hub_tenancy.agent_spaces s ON (s.tenant_id,s.agent_id)=(a.tenant_id,a.id)
    WHERE a.workspace_id IS NULL AND NOT a.retired;
GRANT SELECT ON hub_tenancy.workspace_agents TO hub_portal;
RESET ROLE;
DROP POLICY private_runs ON hub_tenancy.agent_runs;
CREATE POLICY private_runs ON hub_tenancy.agent_runs TO hub_portal USING (
    user_id=nullif(current_setting('hub.principal_id',true),'')::uuid AND EXISTS
    (SELECT FROM hub_tenancy.workspaces w WHERE (w.tenant_id,w.id)=(agent_runs.tenant_id,agent_runs.workspace_id)))
WITH CHECK (user_id=nullif(current_setting('hub.principal_id',true),'')::uuid AND EXISTS
    (SELECT FROM hub_tenancy.workspace_agents a JOIN hub_tenancy.workspaces w ON (w.tenant_id,w.id)=(a.tenant_id,a.workspace_id)
     WHERE (a.tenant_id,a.workspace_id,a.id)=(agent_runs.tenant_id,agent_runs.workspace_id,agent_runs.agent_id) AND a.active));
