ALTER TABLE hub_tenancy.agents DROP CONSTRAINT agents_kind_check,
    ADD CONSTRAINT agents_kind_check CHECK (kind IN (
        'guide', 'knowledge', 'external', 'connector', 'notifications', 'avihub', 'library', 'iaves'
    )),
    ADD CONSTRAINT agents_iaves_identity CHECK (
        (id = '2bf224be-5d7a-4c30-9a89-19d76e28cbb8') = (kind = 'iaves')
        AND (kind <> 'iaves' OR (tenant_id = 'e7eb1e21-00c7-4abd-8ada-641ce20a91b3' AND workspace_id IS NULL))
    );

INSERT INTO hub_tenancy.agents (id, tenant_id, name, kind, description)
SELECT '2bf224be-5d7a-4c30-9a89-19d76e28cbb8', i.tenant_id, 'IAves', 'iaves',
    'Assistente da cadeia de avicultura com conhecimento da Peso Exato.'
FROM hub_tenancy.institutions i WHERE i.tenant_id = 'e7eb1e21-00c7-4abd-8ada-641ce20a91b3'
ON CONFLICT DO NOTHING;

INSERT INTO hub_tenancy.agent_spaces (tenant_id, workspace_id, agent_id)
SELECT w.tenant_id, w.id, a.id FROM hub_tenancy.workspaces w
JOIN hub_tenancy.agents a ON a.tenant_id = w.tenant_id AND a.kind = 'iaves'
WHERE (w.slug = 'principal' OR w.client_onboarding) AND NOT a.retired
ON CONFLICT DO NOTHING;

GRANT INSERT ON hub_tenancy.agent_spaces TO hub_provisioning_code;
CREATE POLICY seed_iaves ON hub_tenancy.agent_spaces FOR INSERT TO hub_provisioning_code WITH CHECK (
    tenant_id = 'e7eb1e21-00c7-4abd-8ada-641ce20a91b3'
    AND agent_id = '2bf224be-5d7a-4c30-9a89-19d76e28cbb8'
    AND EXISTS (SELECT FROM hub_tenancy.workspaces w
                WHERE (w.tenant_id, w.id) = (agent_spaces.tenant_id, agent_spaces.workspace_id)
                  AND (w.slug = 'principal' OR w.client_onboarding))
);

SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.seed_iaves_workspace() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE
    organization constant uuid := 'e7eb1e21-00c7-4abd-8ada-641ce20a91b3';
    agent constant uuid := '2bf224be-5d7a-4c30-9a89-19d76e28cbb8';
BEGIN
    IF NEW.tenant_id <> organization OR (NEW.slug <> 'principal' AND NOT NEW.client_onboarding) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND (OLD.slug = 'principal' OR OLD.client_onboarding) THEN
        RETURN NEW;
    END IF;
    INSERT INTO hub_tenancy.agents (id, tenant_id, name, kind, description)
    SELECT agent, i.tenant_id, 'IAves', 'iaves', 'Assistente da cadeia de avicultura com conhecimento da Peso Exato.'
    FROM hub_tenancy.institutions i WHERE i.tenant_id = organization
    ON CONFLICT DO NOTHING;
    INSERT INTO hub_tenancy.agent_spaces (tenant_id, workspace_id, agent_id)
    SELECT a.tenant_id, NEW.id, a.id FROM hub_tenancy.agents a
    WHERE a.tenant_id = organization AND a.id = agent AND NOT a.retired
    ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION hub_admin.seed_iaves_workspace() FROM PUBLIC;
RESET ROLE;

CREATE TRIGGER seed_iaves_workspace AFTER INSERT OR UPDATE OF client_onboarding ON hub_tenancy.workspaces
    FOR EACH ROW EXECUTE FUNCTION hub_admin.seed_iaves_workspace();
