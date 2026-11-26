-- The guide now belongs to the authenticated account. Keep former rows for run history.
DROP TRIGGER workspace_catalog ON hub_tenancy.workspaces;
DROP FUNCTION hub_admin.seed_workspace_agents();

DELETE FROM hub_tenancy.agent_spaces s USING hub_tenancy.agents a
WHERE (s.tenant_id,s.agent_id)=(a.tenant_id,a.id) AND a.kind='guide';
UPDATE hub_tenancy.agents SET retired=true,active=false WHERE kind='guide';
ALTER TABLE hub_tenancy.agents ADD CONSTRAINT workspace_guides_are_historical
    CHECK (kind <> 'guide' OR (retired AND NOT active));

DO $$
BEGIN
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_schema='hub_accounts'
               AND table_name='bridge_ticket' AND column_name='notice_agent') THEN
        UPDATE hub_accounts.bridge_ticket t SET state='stopped',notice_due=NULL
        FROM hub_tenancy.agents a WHERE t.notice_agent=a.id AND a.kind='guide' AND t.state='pending';
    END IF;
END $$;
