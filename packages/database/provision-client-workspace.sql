-- Run after Django migrations and institutional connector provisioning.
DO $$
DECLARE
    organization constant uuid := 'e7eb1e21-00c7-4abd-8ada-641ce20a91b3';
    owner_id_ uuid;
    space_id uuid;
    library_id uuid;
    avi_id uuid;
BEGIN
    SELECT i.owner_id INTO owner_id_ FROM hub_tenancy.institutions i WHERE i.tenant_id=organization FOR UPDATE;
    IF NOT FOUND OR EXISTS(SELECT FROM hub_tenancy.workspaces WHERE tenant_id=organization AND client_onboarding) THEN
        RETURN;
    END IF;
    SELECT l.id INTO library_id FROM hub_accounts.bridge_notelibrary l
        JOIN hub_tenancy.agents a ON a.id=l.id AND a.tenant_id=l.connection_id
        WHERE l.connection_id=organization AND l.repository_id=1306809360 AND a.kind='library' AND a.active AND NOT a.retired;
    SELECT a.id INTO avi_id FROM hub_tenancy.agents a
        JOIN hub_accounts.bridge_avihubagent v ON v.id=a.id AND v.tenant=a.tenant_id
        WHERE a.tenant_id=organization AND a.kind='avihub' AND a.active AND NOT a.retired;

    IF library_id IS NULL OR avi_id IS NULL THEN
        RAISE EXCEPTION 'Configure os agentes AviHUB e documentação do repositório pesoexatobalancas/Obsidian antes de liberar o espaço de clientes.';
    END IF;
    INSERT INTO hub_tenancy.workspaces(tenant_id,name,slug,client_onboarding)
        VALUES(organization,'Clientes Peso Exato','clientes-peso-exato',true) RETURNING id INTO space_id;
    INSERT INTO hub_tenancy.workspace_members(tenant_id,workspace_id,user_id) VALUES(organization,space_id,owner_id_);
    INSERT INTO hub_tenancy.agent_spaces(tenant_id,workspace_id,agent_id)
        VALUES(organization,space_id,avi_id),(organization,space_id,library_id);
    INSERT INTO hub_accounts.bridge_libraryaccess(library_id,space,folders)
        VALUES(library_id,space_id,'["EQUIPMENTS/Manuais"]'::jsonb);
END $$;
