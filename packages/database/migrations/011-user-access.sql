SET LOCAL ROLE hub_schema_owner;
CREATE TABLE hub_tenancy.user_agent_blocks (
    user_id uuid NOT NULL REFERENCES hub_identity.users,
    agent_id uuid NOT NULL REFERENCES hub_tenancy.agents,
    PRIMARY KEY(user_id,agent_id)
);
ALTER TABLE hub_tenancy.user_agent_blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE hub_tenancy.user_agent_blocks FORCE ROW LEVEL SECURITY;
CREATE POLICY own_blocks ON hub_tenancy.user_agent_blocks FOR SELECT TO hub_portal
    USING (user_id=nullif(current_setting('hub.principal_id',true),'')::uuid);
CREATE POLICY admin_code ON hub_tenancy.user_agent_blocks TO hub_provisioning_code USING (true) WITH CHECK (true);
GRANT SELECT ON hub_tenancy.user_agent_blocks TO hub_portal;
GRANT SELECT,INSERT,DELETE ON hub_tenancy.user_agent_blocks TO hub_provisioning_code;
-- Workspace catalogs use a security-invoker view. The same filter protects direct
-- reads of agent definitions in editors, including for a workspace manager.
CREATE POLICY user_agent_access ON hub_tenancy.agents AS RESTRICTIVE FOR SELECT TO hub_portal
    USING (NOT EXISTS (SELECT FROM hub_tenancy.user_agent_blocks b WHERE b.agent_id=agents.id
        AND b.user_id=nullif(current_setting('hub.principal_id',true),'')::uuid));
RESET ROLE;
GRANT SELECT ON hub_tenancy.agents TO hub_provisioning_code;
CREATE POLICY admin_read ON hub_tenancy.agents FOR SELECT TO hub_provisioning_code USING (true);
GRANT UPDATE(display_name) ON hub_identity.users TO hub_provisioning_code;
GRANT DELETE,UPDATE(active) ON hub_tenancy.memberships TO hub_provisioning_code;
SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.register_personal_account(subject_id bigint,label text) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
    SELECT hub_admin.register_identity('urn:hubia:portal:local',subject_id::text,label,false)
$$;
CREATE FUNCTION hub_admin.account_target(subject_id bigint, deleting boolean DEFAULT false) RETURNS uuid
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE target uuid;
BEGIN
    PERFORM hub_admin.require_platform_owner();
    SELECT user_id INTO STRICT target FROM hub_identity.identities
      WHERE issuer='urn:hubia:portal:local' AND subject=subject_id::text;
    IF deleting AND (EXISTS(SELECT FROM hub_identity.users WHERE id=target AND platform_owner)
        OR EXISTS(SELECT FROM hub_tenancy.institutions WHERE owner_id=target)) THEN
        RAISE EXCEPTION 'O responsável pela plataforma ou por uma instituição não pode ser excluído.' USING ERRCODE='22023';
    END IF;
    RETURN target;
END $$;
CREATE FUNCTION hub_admin.account_options(subject_id bigint)
RETURNS TABLE(category text,tenant uuid,id uuid,label text,selected boolean,locked boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE target uuid := hub_admin.account_target(subject_id);
BEGIN
    RETURN QUERY
    SELECT 'institution'::text,i.tenant_id,i.tenant_id,i.name,coalesce(m.active,false),i.owner_id=target
      FROM hub_tenancy.institutions i JOIN hub_tenancy.tenants t ON t.id=i.tenant_id
      LEFT JOIN hub_tenancy.memberships m ON m.tenant_id=i.tenant_id AND m.user_id=target WHERE t.status='ACTIVE'
    UNION ALL
    SELECT 'space',w.tenant_id,w.id,w.name,wm.user_id IS NOT NULL,i.owner_id=target
      FROM hub_tenancy.workspaces w JOIN hub_tenancy.institutions i USING(tenant_id)
      JOIN hub_tenancy.tenants t ON t.id=w.tenant_id LEFT JOIN hub_tenancy.workspace_members wm
      ON (wm.tenant_id,wm.workspace_id,wm.user_id)=(w.tenant_id,w.id,target)
      WHERE w.deleted_at IS NULL AND t.status='ACTIVE'
    UNION ALL
    SELECT 'agent',a.tenant_id,a.id,a.name,b.user_id IS NULL,false FROM hub_tenancy.agents a
      JOIN hub_tenancy.tenants t ON t.id=a.tenant_id
      LEFT JOIN hub_tenancy.personal_spaces p ON p.tenant_id=a.tenant_id
      LEFT JOIN hub_tenancy.user_agent_blocks b ON b.agent_id=a.id AND b.user_id=target
      WHERE NOT a.retired AND t.status='ACTIVE' AND (t.kind='ORGANIZATION' OR p.owner_id=target);
END $$;
CREATE FUNCTION hub_admin.change_account_access(subject_id bigint,item_type text,item uuid,enabled boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE target uuid := hub_admin.account_target(subject_id); opt record;
BEGIN
    SELECT * INTO opt FROM hub_admin.account_options(subject_id) o WHERE o.category=item_type AND o.id=item;
    IF NOT FOUND OR opt.locked THEN RAISE EXCEPTION 'Opção indisponível.' USING ERRCODE='22023'; END IF;
    IF item_type='institution' THEN
        IF enabled THEN
            INSERT INTO hub_tenancy.memberships(tenant_id,user_id,role) VALUES(item,target,'member')
            ON CONFLICT(tenant_id,user_id) DO UPDATE SET active=true;
        ELSE
            DELETE FROM hub_tenancy.workspace_members WHERE tenant_id=item AND user_id=target;
            DELETE FROM hub_tenancy.memberships WHERE tenant_id=item AND user_id=target;
        END IF;
    ELSIF item_type='space' THEN
        IF enabled THEN
            IF NOT EXISTS(SELECT FROM hub_tenancy.memberships WHERE tenant_id=opt.tenant AND user_id=target AND active) THEN
                RAISE EXCEPTION 'Selecione a instituição do espaço.' USING ERRCODE='22023';
            END IF;
            INSERT INTO hub_tenancy.workspace_members(tenant_id,workspace_id,user_id) VALUES(opt.tenant,item,target) ON CONFLICT DO NOTHING;
        ELSE
            DELETE FROM hub_tenancy.workspace_members WHERE tenant_id=opt.tenant AND workspace_id=item AND user_id=target;
        END IF;
    ELSE
        IF enabled THEN DELETE FROM hub_tenancy.user_agent_blocks WHERE user_id=target AND agent_id=item;
        ELSE INSERT INTO hub_tenancy.user_agent_blocks VALUES(target,item) ON CONFLICT DO NOTHING; END IF;
    END IF;
    INSERT INTO hub_tenancy.audit_logs(tenant_id,actor_id,action) VALUES(opt.tenant,hub_admin.require_platform_owner(),
        concat_ws(':','account_access',target,item_type,item,enabled));
END $$;
CREATE FUNCTION hub_admin.change_account_name(subject_id bigint,label text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE target uuid := hub_admin.account_target(subject_id);
BEGIN
    UPDATE hub_identity.users SET display_name=label WHERE id=target;
END $$;
REVOKE ALL ON FUNCTION hub_admin.change_account_name(bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.change_account_name(bigint,text) TO hub_portal;
CREATE FUNCTION hub_admin.may_receive_agent_notice(subject_id bigint, agent uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
    SELECT EXISTS(SELECT FROM hub_identity.identities i JOIN hub_identity.users u ON u.id=i.user_id AND u.active
       WHERE i.issuer='urn:hubia:portal:local' AND i.subject=subject_id::text
       AND NOT EXISTS (SELECT FROM hub_tenancy.user_agent_blocks b WHERE b.user_id=u.id AND b.agent_id=agent))
$$;
REVOKE ALL ON FUNCTION hub_admin.may_receive_agent_notice(bigint,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.may_receive_agent_notice(bigint,uuid) TO hub_portal;
REVOKE ALL ON FUNCTION hub_admin.register_personal_account(bigint,text),hub_admin.account_target(bigint,boolean),
    hub_admin.account_options(bigint),hub_admin.change_account_access(bigint,text,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.register_personal_account(bigint,text),hub_admin.account_target(bigint,boolean),
    hub_admin.account_options(bigint),hub_admin.change_account_access(bigint,text,uuid,boolean) TO hub_portal;
RESET ROLE;
