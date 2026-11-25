ALTER TABLE hub_tenancy.agents DROP CONSTRAINT agents_kind_check,
    ADD CONSTRAINT agents_kind_check CHECK (kind IN ('guide','knowledge','external','connector','notifications'));

-- Provision only the existing institutional workspace requested by its owner.
INSERT INTO hub_tenancy.agents (tenant_id,workspace_id,name,kind,description)
SELECT tenant_id,id,'Notificações Peso Exato','notifications',
       'Envie avisos pelos canais e destinos habilitados para o seu setor.'
FROM hub_tenancy.workspaces
WHERE tenant_id='e7eb1e21-00c7-4abd-8ada-641ce20a91b3' AND slug='principal'
ON CONFLICT (tenant_id,workspace_id,name) DO NOTHING;
