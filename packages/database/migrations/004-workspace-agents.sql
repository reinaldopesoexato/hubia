GRANT SELECT (kind) ON hub_tenancy.tenants TO hub_provisioning_code;
SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.can_manage_space(t uuid, s uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM hub_tenancy.workspaces w JOIN hub_tenancy.tenants x ON x.id=w.tenant_id
        JOIN hub_identity.users u ON u.id=nullif(current_setting('hub.principal_id',true),'')::uuid AND u.active
        WHERE w.tenant_id=t AND w.id=s AND x.status='ACTIVE'
        AND ((u.platform_owner AND x.kind='ORGANIZATION') OR EXISTS (
            SELECT FROM hub_tenancy.memberships m JOIN hub_tenancy.workspace_members a
            ON (m.tenant_id,m.user_id)=(a.tenant_id,a.user_id)
            WHERE m.tenant_id=t AND a.workspace_id=s AND m.user_id=u.id AND m.active
            AND m.role IN ('personal_owner','institution_owner','institution_admin'))))
$$;
CREATE FUNCTION hub_admin.space_management(t uuid,s uuid)
RETURNS TABLE (organization text, workspace text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
    SELECT coalesce(i.name,'Espaço pessoal'), w.name FROM hub_tenancy.workspaces w
    LEFT JOIN hub_tenancy.institutions i ON i.tenant_id=w.tenant_id
    WHERE w.tenant_id=t AND w.id=s AND hub_admin.can_manage_space(t,s)
$$;
CREATE FUNCTION hub_admin.space_people(t uuid,s uuid)
RETURNS TABLE (name text,role text) LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
    SELECT u.display_name,m.role FROM hub_tenancy.workspace_members a
    JOIN hub_identity.users u ON a.user_id=u.id AND u.active
    JOIN hub_tenancy.memberships m ON (m.tenant_id,m.user_id)=(a.tenant_id,a.user_id) AND m.active
    WHERE a.tenant_id=t AND a.workspace_id=s AND hub_admin.can_manage_space(t,s) ORDER BY lower(u.display_name)
$$;
CREATE FUNCTION hub_admin.register_portal_user(subject_id text,username text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid := hub_admin.require_platform_owner(); target_id uuid;
BEGIN
    target_id := hub_admin.register_identity('urn:hubia:portal:local',subject_id,username,false);
    INSERT INTO hub_tenancy.audit_logs(tenant_id,actor_id,action)
    SELECT tenant_id,actor,concat_ws(':','platform','account_registered',target_id)
    FROM hub_tenancy.personal_spaces WHERE owner_id=actor;
    RETURN target_id;
END $$;
REVOKE ALL ON FUNCTION hub_admin.can_manage_space(uuid,uuid),hub_admin.space_people(uuid,uuid),
  hub_admin.space_management(uuid,uuid),hub_admin.register_portal_user(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.can_manage_space(uuid,uuid),hub_admin.space_people(uuid,uuid),
  hub_admin.space_management(uuid,uuid),hub_admin.register_portal_user(text,text) TO hub_portal;
RESET ROLE;

SET LOCAL ROLE hub_schema_owner;
CREATE TABLE hub_tenancy.agents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL, workspace_id uuid NOT NULL,
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    kind text NOT NULL CHECK (kind IN ('guide','knowledge','external','connector')),
    description text NOT NULL DEFAULT '' CHECK(length(description)<=500),
    configuration text NOT NULL DEFAULT '' CHECK(length(configuration)<=16000),
    active boolean NOT NULL DEFAULT true,
    UNIQUE(tenant_id,workspace_id,name), UNIQUE(tenant_id,workspace_id,id),
    FOREIGN KEY (tenant_id,workspace_id) REFERENCES hub_tenancy.workspaces
);
CREATE TABLE hub_tenancy.agent_runs (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL, workspace_id uuid NOT NULL, agent_id uuid NOT NULL,
    user_id uuid NOT NULL REFERENCES hub_identity.users,
    question text NOT NULL CHECK(length(question) BETWEEN 1 AND 2000),
    answer text NOT NULL DEFAULT '', status text NOT NULL CHECK(status IN ('running','done','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,workspace_id,agent_id) REFERENCES hub_tenancy.agents(tenant_id,workspace_id,id)
);
CREATE INDEX agent_history ON hub_tenancy.agent_runs(tenant_id,workspace_id,user_id,agent_id,created_at DESC);
RESET ROLE;
INSERT INTO hub_tenancy.agents(tenant_id,workspace_id,name,kind,description)
SELECT tenant_id,id,'Guia do espaço','guide','Consulte os agentes disponíveis e as permissões da sua conta neste espaço.'
FROM hub_tenancy.workspaces;
GRANT INSERT ON hub_tenancy.agents TO hub_provisioning_code;
SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.seed_workspace_agents() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    INSERT INTO hub_tenancy.agents(tenant_id,workspace_id,name,kind,description)
    VALUES(NEW.tenant_id,NEW.id,'Guia do espaço','guide','Consulte os agentes disponíveis e as permissões da sua conta neste espaço.');
    RETURN NEW;
END $$;
CREATE FUNCTION hub_admin.audit_workspace_agent() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid := nullif(current_setting('hub.principal_id',true),'')::uuid;
BEGIN
    IF actor IS NOT NULL THEN
        INSERT INTO hub_tenancy.audit_logs(tenant_id,actor_id,action)
        VALUES(NEW.tenant_id,actor,concat_ws(':','agent',TG_OP,NEW.workspace_id,NEW.id));
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION hub_admin.seed_workspace_agents(),hub_admin.audit_workspace_agent() FROM PUBLIC;
RESET ROLE;
CREATE TRIGGER workspace_catalog AFTER INSERT ON hub_tenancy.workspaces
FOR EACH ROW EXECUTE FUNCTION hub_admin.seed_workspace_agents();
CREATE TRIGGER agent_audit AFTER INSERT OR UPDATE ON hub_tenancy.agents
FOR EACH ROW EXECUTE FUNCTION hub_admin.audit_workspace_agent();
ALTER TABLE hub_tenancy.agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE hub_tenancy.agents FORCE ROW LEVEL SECURITY;
ALTER TABLE hub_tenancy.agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE hub_tenancy.agent_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY seed ON hub_tenancy.agents FOR INSERT TO hub_provisioning_code WITH CHECK (true);
CREATE POLICY members_read ON hub_tenancy.agents FOR SELECT TO hub_portal USING
    (active AND EXISTS (SELECT FROM hub_tenancy.workspaces w WHERE (w.tenant_id,w.id)=(agents.tenant_id,agents.workspace_id)));
CREATE POLICY manage ON hub_tenancy.agents TO hub_portal USING
    (hub_admin.can_manage_space(tenant_id,workspace_id)) WITH CHECK(hub_admin.can_manage_space(tenant_id,workspace_id));
CREATE POLICY private_runs ON hub_tenancy.agent_runs TO hub_portal USING (
    user_id=nullif(current_setting('hub.principal_id',true),'')::uuid AND EXISTS
    (SELECT FROM hub_tenancy.workspaces w WHERE (w.tenant_id,w.id)=(agent_runs.tenant_id,agent_runs.workspace_id)))
WITH CHECK (
    user_id=nullif(current_setting('hub.principal_id',true),'')::uuid AND EXISTS
    (SELECT FROM hub_tenancy.workspaces w WHERE (w.tenant_id,w.id)=(agent_runs.tenant_id,agent_runs.workspace_id))
    AND EXISTS (SELECT FROM hub_tenancy.agents a WHERE a.id=agent_id AND a.active));
GRANT SELECT,INSERT,UPDATE(name,kind,description,configuration,active) ON hub_tenancy.agents TO hub_portal;
GRANT SELECT,INSERT,UPDATE(answer,status) ON hub_tenancy.agent_runs TO hub_portal;
GRANT UPDATE (active) ON hub_identity.users TO hub_provisioning_code;
GRANT SELECT ON hub_tenancy.audit_logs TO hub_provisioning_code;
SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.set_portal_user_active(subject_id text, enabled boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid := hub_admin.require_platform_owner(); target_id uuid;
BEGIN
    SELECT i.user_id INTO target_id FROM hub_identity.identities i
    WHERE i.issuer='urn:hubia:portal:local' AND i.subject=subject_id;
    UPDATE hub_identity.users SET active=enabled WHERE id=target_id AND NOT platform_owner AND id<>actor;
    IF NOT FOUND THEN RAISE EXCEPTION 'A conta não pode ser alterada.' USING ERRCODE='22023'; END IF;
    INSERT INTO hub_tenancy.audit_logs (tenant_id,actor_id,action)
    SELECT tenant_id,actor,concat_ws(':','platform','account_active',target_id,enabled)
    FROM hub_tenancy.personal_spaces WHERE owner_id=actor;
END $$;
CREATE FUNCTION hub_admin.technical_events() RETURNS TABLE (occurred timestamptz, institution text, actor text, event text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE v_platform_actor uuid := hub_admin.require_platform_owner();
BEGIN
    RETURN QUERY SELECT l.created_at,coalesce(i.name,'Plataforma'),u.display_name,l.action
    FROM hub_tenancy.audit_logs l LEFT JOIN hub_tenancy.institutions i ON l.tenant_id=i.tenant_id
    JOIN hub_identity.users u ON u.id=l.actor_id
    WHERE i.tenant_id IS NOT NULL OR (l.actor_id=v_platform_actor AND l.action LIKE 'platform%')
    ORDER BY l.created_at DESC,l.id DESC LIMIT 100;
END $$;
REVOKE ALL ON FUNCTION hub_admin.set_portal_user_active(text,boolean),hub_admin.technical_events() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.set_portal_user_active(text,boolean),hub_admin.technical_events() TO hub_portal;
RESET ROLE;
