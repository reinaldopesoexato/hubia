GRANT SELECT (platform_owner) ON hub_identity.users TO hub_runtime;
GRANT SELECT (name), UPDATE (name) ON hub_tenancy.workspaces TO hub_provisioning_code;
GRANT UPDATE (name) ON hub_tenancy.institutions TO hub_provisioning_code;
GRANT UPDATE (status) ON hub_tenancy.tenants TO hub_provisioning_code;
GRANT SELECT, DELETE ON hub_tenancy.workspace_members TO hub_provisioning_code;

SET LOCAL ROLE hub_provisioning_code;

CREATE FUNCTION hub_admin.require_platform_owner() RETURNS uuid
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := nullif(current_setting('hub.principal_id', true), '')::uuid;
BEGIN
    IF NOT EXISTS (SELECT FROM hub_identity.users WHERE id = actor AND active AND platform_owner) THEN
        RAISE EXCEPTION 'Acesso administrativo necessário' USING ERRCODE = '42501';
    END IF;
    RETURN actor;
END
$$;

CREATE FUNCTION hub_admin.organization_directory()
RETURNS TABLE (tenant_id uuid, organization text, status text, workspace_id uuid, workspace text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM hub_admin.require_platform_owner();
    RETURN QUERY
        SELECT i.tenant_id, i.name, t.status, w.id, w.name
        FROM hub_tenancy.institutions i JOIN hub_tenancy.tenants t ON t.id = i.tenant_id
        LEFT JOIN hub_tenancy.workspaces w ON w.tenant_id = i.tenant_id
        ORDER BY lower(i.name), i.tenant_id, lower(w.name), w.id;
END
$$;

CREATE FUNCTION hub_admin.manage_organization(
    action text, tenant uuid, workspace uuid, label text, slug text, target_user uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    actor uuid := hub_admin.require_platform_owner();
    institution_owner uuid;
    new_space uuid;
    resource uuid;
    current_status text;
BEGIN
    IF action IN ('create_org', 'rename_org', 'create_space', 'rename_space')
       AND (label IS NULL OR length(btrim(label)) NOT BETWEEN 1 AND 120) THEN
        RAISE EXCEPTION 'Informe um nome de até 120 caracteres.' USING ERRCODE = '22023';
    END IF;
    IF action IN ('create_org', 'create_space') AND
       (slug IS NULL OR length(slug) > 80 OR slug !~ '^[a-z0-9]+(-[a-z0-9]+)*$') THEN
        RAISE EXCEPTION 'Identificador inválido.' USING ERRCODE = '22023';
    END IF;
    IF action = 'create_org' THEN
        PERFORM hub_admin.provision_institution(actor, coalesce(target_user, actor), slug, btrim(label));
        RETURN;
    END IF;
    SELECT i.owner_id, t.status INTO institution_owner, current_status
        FROM hub_tenancy.institutions i JOIN hub_tenancy.tenants t ON t.id = i.tenant_id
        WHERE i.tenant_id = tenant FOR UPDATE OF t;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Instituição desconhecida.' USING ERRCODE = '22023';
    END IF;
    IF current_status = 'SUSPENDED' AND action <> 'activate' THEN
        RAISE EXCEPTION 'Reative a instituição antes de alterá-la.' USING ERRCODE = '22023';
    END IF;
    CASE action
        WHEN 'rename_org' THEN
            UPDATE hub_tenancy.institutions SET name = btrim(label) WHERE tenant_id = tenant;
        WHEN 'suspend', 'activate' THEN
            UPDATE hub_tenancy.tenants SET status = CASE action WHEN 'activate' THEN 'ACTIVE' ELSE 'SUSPENDED' END
                WHERE id = tenant;
        WHEN 'create_space' THEN
            INSERT INTO hub_tenancy.workspaces (tenant_id, name, slug)
                VALUES (tenant, btrim(label), slug) RETURNING id INTO new_space;
            INSERT INTO hub_tenancy.workspace_members (tenant_id, workspace_id, user_id)
                VALUES (tenant, new_space, institution_owner);
            resource := new_space;
        WHEN 'rename_space' THEN
            UPDATE hub_tenancy.workspaces SET name = btrim(label) WHERE tenant_id = tenant AND id = workspace;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Espaço desconhecido.' USING ERRCODE = '22023';
            END IF;
            resource := workspace;
        WHEN 'revoke_space' THEN
            IF target_user = institution_owner THEN
                RAISE EXCEPTION 'O responsável pela instituição mantém acesso ao espaço.' USING ERRCODE = '22023';
            END IF;
            DELETE FROM hub_tenancy.workspace_members
                WHERE tenant_id = tenant AND workspace_id = workspace AND user_id = target_user;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Este usuário não possui acesso ao espaço.' USING ERRCODE = '22023';
            END IF;
            resource := workspace;
        WHEN 'grant_space' THEN
            IF NOT EXISTS (SELECT FROM hub_tenancy.workspaces WHERE tenant_id = tenant AND id = workspace)
               OR NOT EXISTS (SELECT FROM hub_identity.users WHERE id = target_user AND active) THEN
                RAISE EXCEPTION 'Espaço ou usuário inválido.' USING ERRCODE = '22023';
            END IF;
            INSERT INTO hub_tenancy.memberships (tenant_id,user_id,role)
                VALUES (tenant,target_user,'member') ON CONFLICT DO NOTHING;
            IF NOT EXISTS (SELECT FROM hub_tenancy.memberships WHERE tenant_id = tenant AND user_id = target_user AND active) THEN
                RAISE EXCEPTION 'Vínculo desativado.' USING ERRCODE = '22023';
            END IF;
            INSERT INTO hub_tenancy.workspace_members (tenant_id,workspace_id,user_id)
                VALUES (tenant,workspace,target_user) ON CONFLICT DO NOTHING;
            resource := workspace;
        ELSE
            RAISE EXCEPTION 'Operação desconhecida.' USING ERRCODE = '22023';
    END CASE;
    INSERT INTO hub_tenancy.audit_logs (tenant_id, actor_id, action)
        VALUES (tenant, actor, concat_ws(':','platform',action,resource,target_user));
END
$$;

REVOKE ALL ON FUNCTION hub_admin.require_platform_owner(), hub_admin.organization_directory(),
    hub_admin.manage_organization(text,uuid,uuid,text,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.organization_directory(),
    hub_admin.manage_organization(text,uuid,uuid,text,text,uuid) TO hub_portal;
RESET ROLE;
