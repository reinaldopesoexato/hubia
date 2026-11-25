GRANT UPDATE(role) ON hub_tenancy.memberships TO hub_provisioning_code;
SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.manage_member(t uuid,s uuid,chosen uuid,operation text,new_role text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE actor uuid := nullif(current_setting('hub.principal_id',true),'')::uuid;
        responsible uuid; existing_role text; global_owner boolean;
BEGIN
    SELECT i.owner_id INTO responsible FROM hub_tenancy.institutions i JOIN hub_tenancy.tenants x ON x.id=i.tenant_id
    WHERE x.id=t AND x.status='ACTIVE' FOR UPDATE OF x;
    IF NOT FOUND OR NOT hub_admin.can_manage_space(t,s) THEN
        RAISE EXCEPTION 'Gestão indisponível para este espaço.' USING ERRCODE='42501';
    END IF;
    SELECT platform_owner INTO global_owner FROM hub_identity.users WHERE id=actor AND active;
    IF chosen=responsible OR NOT EXISTS (SELECT FROM hub_identity.users WHERE id=chosen AND active) THEN
        RAISE EXCEPTION 'Escolha um membro ativo diferente do responsável institucional.' USING ERRCODE='22023';
    END IF;
    SELECT role INTO existing_role FROM hub_tenancy.memberships WHERE tenant_id=t AND user_id=chosen AND active;
    CASE operation
    WHEN 'grant' THEN
        INSERT INTO hub_tenancy.memberships(tenant_id,user_id,role) VALUES(t,chosen,'member') ON CONFLICT DO NOTHING;
        IF NOT EXISTS (SELECT FROM hub_tenancy.memberships WHERE tenant_id=t AND user_id=chosen AND active) THEN
            RAISE EXCEPTION 'Vínculo institucional desativado.' USING ERRCODE='22023';
        END IF;
        INSERT INTO hub_tenancy.workspace_members(tenant_id,workspace_id,user_id) VALUES(t,s,chosen) ON CONFLICT DO NOTHING;
    WHEN 'revoke' THEN
        IF existing_role='institution_admin' AND NOT (global_owner OR actor=responsible) THEN
            RAISE EXCEPTION 'Somente o responsável pode remover outro administrador.' USING ERRCODE='42501';
        END IF;
        DELETE FROM hub_tenancy.workspace_members WHERE tenant_id=t AND workspace_id=s AND user_id=chosen;
        IF NOT FOUND THEN RAISE EXCEPTION 'Usuário sem acesso ao espaço.' USING ERRCODE='22023'; END IF;
    WHEN 'role' THEN
        IF NOT (global_owner OR actor=responsible) THEN
            RAISE EXCEPTION 'O papel institucional exige o responsável ou administrador geral.' USING ERRCODE='42501';
        END IF;
        IF new_role IS NULL OR new_role NOT IN ('member','institution_admin','viewer') OR NOT EXISTS
          (SELECT FROM hub_tenancy.workspace_members WHERE tenant_id=t AND workspace_id=s AND user_id=chosen) THEN
            RAISE EXCEPTION 'Escolha um membro deste espaço e um papel válido.' USING ERRCODE='22023';
        END IF;
        UPDATE hub_tenancy.memberships SET role=new_role WHERE tenant_id=t AND user_id=chosen AND active;
    ELSE RAISE EXCEPTION 'Operação inválida.' USING ERRCODE='22023';
    END CASE;
    INSERT INTO hub_tenancy.audit_logs(tenant_id,actor_id,action)
    VALUES(t,actor,concat_ws(':','membership',operation,s,chosen,new_role));
END $$;
REVOKE ALL ON FUNCTION hub_admin.manage_member(uuid,uuid,uuid,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.manage_member(uuid,uuid,uuid,text,text) TO hub_portal;
RESET ROLE;
