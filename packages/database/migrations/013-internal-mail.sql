ALTER TABLE hub_tenancy.memberships ADD COLUMN internal_mail boolean NOT NULL DEFAULT false;

-- Preserve existing Peso Exato staff. Future workspace grants do not grant mail.
UPDATE hub_tenancy.memberships m SET internal_mail=true
WHERE m.tenant_id='e7eb1e21-00c7-4abd-8ada-641ce20a91b3' AND m.active
  AND (m.role IN ('institution_owner','institution_admin') OR EXISTS (
      SELECT FROM hub_tenancy.workspace_members wm JOIN hub_tenancy.workspaces w
          ON (w.tenant_id,w.id)=(wm.tenant_id,wm.workspace_id)
      WHERE wm.tenant_id=m.tenant_id AND wm.user_id=m.user_id AND w.deleted_at IS NULL
        AND NOT w.client_onboarding AND w.id IN (
            '6b364cc2-8afd-4833-b542-875dafeaf8c2','c5ccf032-4494-44ca-b1d6-ba0f914d81ed',
            'b8786757-c61e-4831-b0ee-8de8d54023ab','5d290726-171d-4583-b100-8b8da15bfd89'
        )
  ));

GRANT UPDATE(internal_mail) ON hub_tenancy.memberships TO hub_provisioning_code;
SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.account_internal_mail(subject_id bigint, enabled boolean DEFAULT NULL) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE
    target uuid := hub_admin.account_target(subject_id);
    institution constant uuid := 'e7eb1e21-00c7-4abd-8ada-641ce20a91b3';
BEGIN
    IF enabled IS NOT NULL THEN
        UPDATE hub_tenancy.memberships SET internal_mail=enabled
            WHERE tenant_id=institution AND user_id=target AND active;
        IF NOT FOUND AND enabled THEN
            RAISE EXCEPTION 'Vincule o usuário à instituição Peso Exato para liberar o e-mail interno.' USING ERRCODE='22023';
        END IF;
        INSERT INTO hub_tenancy.audit_logs(tenant_id,actor_id,action) VALUES(
            institution,hub_admin.require_platform_owner(),concat_ws(':','internal_mail',target,enabled)
        );
    END IF;
    RETURN EXISTS (SELECT FROM hub_tenancy.memberships
                   WHERE tenant_id=institution AND user_id=target AND active AND internal_mail);
END $$;
REVOKE ALL ON FUNCTION hub_admin.account_internal_mail(bigint,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hub_admin.account_internal_mail(bigint,boolean) TO hub_portal;
RESET ROLE;
