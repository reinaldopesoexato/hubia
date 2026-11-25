CREATE ROLE hub_schema_owner NOLOGIN NOINHERIT NOBYPASSRLS;
CREATE ROLE hub_runtime NOLOGIN NOINHERIT NOBYPASSRLS;
CREATE ROLE hub_provisioning_code NOLOGIN NOINHERIT NOBYPASSRLS;
CREATE ROLE hub_operator NOLOGIN NOINHERIT NOBYPASSRLS;

CREATE SCHEMA hub_identity AUTHORIZATION hub_schema_owner;
CREATE SCHEMA hub_tenancy AUTHORIZATION hub_schema_owner;
CREATE SCHEMA hub_admin AUTHORIZATION hub_provisioning_code;

SET LOCAL ROLE hub_schema_owner;

CREATE TABLE hub_identity.users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 120),
    active boolean NOT NULL DEFAULT true,
    platform_owner boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX one_platform_owner
    ON hub_identity.users (platform_owner) WHERE platform_owner;

CREATE TABLE hub_identity.identities (
    issuer text NOT NULL CHECK (length(btrim(issuer)) BETWEEN 1 AND 512),
    subject text NOT NULL CHECK (length(btrim(subject)) BETWEEN 1 AND 255),
    user_id uuid NOT NULL REFERENCES hub_identity.users (id),
    PRIMARY KEY (issuer, subject)
);

CREATE TABLE hub_tenancy.tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('ORGANIZATION', 'PERSONAL')),
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'SUSPENDED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, kind)
);

CREATE TABLE hub_tenancy.memberships (
    tenant_id uuid NOT NULL REFERENCES hub_tenancy.tenants (id),
    user_id uuid NOT NULL REFERENCES hub_identity.users (id),
    role text NOT NULL CHECK (role IN (
        'institution_owner', 'institution_admin', 'member', 'viewer', 'personal_owner'
    )),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE INDEX memberships_by_user ON hub_tenancy.memberships (user_id, tenant_id);

CREATE TABLE hub_tenancy.institutions (
    tenant_id uuid PRIMARY KEY,
    kind text NOT NULL DEFAULT 'ORGANIZATION' CHECK (kind = 'ORGANIZATION'),
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 80),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    owner_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, kind) REFERENCES hub_tenancy.tenants (id, kind),
    FOREIGN KEY (tenant_id, owner_id) REFERENCES hub_tenancy.memberships (tenant_id, user_id)
);

CREATE TABLE hub_tenancy.personal_spaces (
    tenant_id uuid PRIMARY KEY,
    kind text NOT NULL DEFAULT 'PERSONAL' CHECK (kind = 'PERSONAL'),
    owner_id uuid NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, kind) REFERENCES hub_tenancy.tenants (id, kind),
    FOREIGN KEY (tenant_id, owner_id) REFERENCES hub_tenancy.memberships (tenant_id, user_id)
);

CREATE TABLE hub_tenancy.workspaces (
    tenant_id uuid NOT NULL REFERENCES hub_tenancy.tenants (id),
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    slug text NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, slug)
);

CREATE TABLE hub_tenancy.workspace_members (
    tenant_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    user_id uuid NOT NULL,
    PRIMARY KEY (tenant_id, workspace_id, user_id),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES hub_tenancy.workspaces (tenant_id, id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES hub_tenancy.memberships (tenant_id, user_id)
);

CREATE TABLE hub_tenancy.audit_logs (
    tenant_id uuid NOT NULL REFERENCES hub_tenancy.tenants (id),
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    actor_id uuid NOT NULL REFERENCES hub_identity.users (id),
    action text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

GRANT USAGE ON SCHEMA hub_identity, hub_tenancy TO hub_provisioning_code;
GRANT USAGE ON SCHEMA hub_identity, hub_tenancy TO hub_runtime;
GRANT SELECT (id, active) ON hub_identity.users TO hub_runtime;
ALTER TABLE hub_identity.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE hub_identity.users FORCE ROW LEVEL SECURITY;
CREATE POLICY runtime_identity ON hub_identity.users TO hub_runtime
    USING (id = nullif(current_setting('hub.principal_id', true), '')::uuid);
CREATE POLICY provisioning_identity ON hub_identity.users TO hub_provisioning_code
    USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON hub_identity.users, hub_identity.identities TO hub_provisioning_code;
GRANT SELECT, INSERT ON hub_tenancy.personal_spaces, hub_tenancy.institutions TO hub_provisioning_code;
GRANT INSERT, SELECT (id, status) ON hub_tenancy.tenants TO hub_provisioning_code;
GRANT INSERT, SELECT (id, tenant_id, slug) ON hub_tenancy.workspaces TO hub_provisioning_code;
GRANT SELECT, INSERT ON hub_tenancy.memberships TO hub_provisioning_code;
GRANT INSERT ON hub_tenancy.workspace_members, hub_tenancy.audit_logs TO hub_provisioning_code;

DO $policies$
DECLARE
    target regclass;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'hub_tenancy.tenants'::regclass, 'hub_tenancy.memberships'::regclass,
        'hub_tenancy.institutions'::regclass, 'hub_tenancy.personal_spaces'::regclass,
        'hub_tenancy.workspaces'::regclass, 'hub_tenancy.workspace_members'::regclass,
        'hub_tenancy.audit_logs'::regclass
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', target);
        EXECUTE format(
            'CREATE POLICY provisioning ON %s TO hub_provisioning_code USING (true) WITH CHECK (true)',
            target
        );
    END LOOP;
END
$policies$;

GRANT SELECT ON hub_tenancy.tenants, hub_tenancy.memberships, hub_tenancy.institutions,
    hub_tenancy.personal_spaces, hub_tenancy.workspaces, hub_tenancy.workspace_members TO hub_runtime;

CREATE POLICY own_membership ON hub_tenancy.memberships FOR SELECT TO hub_runtime
    USING (
        active
        AND user_id = nullif(current_setting('hub.principal_id', true), '')::uuid
        AND EXISTS (SELECT FROM hub_identity.users WHERE id = memberships.user_id AND active)
    );

CREATE POLICY active_tenant ON hub_tenancy.tenants FOR SELECT TO hub_runtime
    USING (
        status = 'ACTIVE' AND id = nullif(current_setting('hub.tenant_id', true), '')::uuid
        AND EXISTS (SELECT FROM hub_tenancy.memberships WHERE tenant_id = tenants.id)
    );

CREATE POLICY institution_scope ON hub_tenancy.institutions FOR SELECT TO hub_runtime
    USING (EXISTS (SELECT FROM hub_tenancy.tenants WHERE id = institutions.tenant_id));

CREATE POLICY personal_scope ON hub_tenancy.personal_spaces FOR SELECT TO hub_runtime
    USING (EXISTS (SELECT FROM hub_tenancy.tenants WHERE id = personal_spaces.tenant_id));

CREATE POLICY own_workspace_membership ON hub_tenancy.workspace_members FOR SELECT TO hub_runtime
    USING (
        user_id = nullif(current_setting('hub.principal_id', true), '')::uuid
        AND EXISTS (SELECT FROM hub_tenancy.tenants WHERE id = workspace_members.tenant_id)
        AND EXISTS (
            SELECT FROM hub_tenancy.memberships AS membership
            WHERE membership.tenant_id = workspace_members.tenant_id
                AND membership.user_id = workspace_members.user_id
        )
    );

CREATE POLICY workspace_scope ON hub_tenancy.workspaces FOR SELECT TO hub_runtime
    USING (EXISTS (
        SELECT FROM hub_tenancy.workspace_members AS member
        WHERE member.tenant_id = workspaces.tenant_id AND member.workspace_id = workspaces.id
    ));

RESET ROLE;
SET LOCAL ROLE hub_provisioning_code;

CREATE FUNCTION hub_admin.create_home(p_tenant uuid, p_owner uuid)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    home_id uuid;
BEGIN
    INSERT INTO hub_tenancy.workspaces (tenant_id, slug, name)
    VALUES (p_tenant, 'principal', 'Principal') RETURNING id INTO home_id;

    INSERT INTO hub_tenancy.workspace_members (tenant_id, workspace_id, user_id)
    VALUES (p_tenant, home_id, p_owner);
END
$$;

-- Operador confiável cadastra identificadores previamente verificados no provedor.
CREATE FUNCTION hub_admin.register_identity(
    p_issuer text, p_subject text, p_name text, p_bootstrap_owner boolean DEFAULT false
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    account_id uuid;
    personal_id uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(164201, 2);
    SELECT identity.user_id INTO account_id FROM hub_identity.identities AS identity
    WHERE identity.issuer = p_issuer AND identity.subject = p_subject;

    IF account_id IS NOT NULL THEN
        IF p_bootstrap_owner AND NOT EXISTS (
            SELECT FROM hub_identity.users WHERE id = account_id AND platform_owner
        ) THEN
            RAISE EXCEPTION 'A identidade existente não é a do proprietário da plataforma';
        END IF;
        RETURN account_id;
    END IF;

    IF p_bootstrap_owner AND EXISTS (SELECT FROM hub_identity.users WHERE platform_owner) THEN
        RAISE EXCEPTION 'O proprietário inicial já foi cadastrado';
    END IF;

    INSERT INTO hub_identity.users (display_name, platform_owner)
    VALUES (p_name, p_bootstrap_owner) RETURNING id INTO account_id;

    INSERT INTO hub_identity.identities (issuer, subject, user_id)
    VALUES (p_issuer, p_subject, account_id);

    INSERT INTO hub_tenancy.tenants (kind) VALUES ('PERSONAL') RETURNING id INTO personal_id;
    INSERT INTO hub_tenancy.memberships (tenant_id, user_id, role)
    VALUES (personal_id, account_id, 'personal_owner');
    INSERT INTO hub_tenancy.personal_spaces (tenant_id, owner_id) VALUES (personal_id, account_id);
    PERFORM hub_admin.create_home(personal_id, account_id);
    INSERT INTO hub_tenancy.audit_logs (tenant_id, actor_id, action)
    VALUES (personal_id, account_id, 'personal_space.provisioned');

    RETURN account_id;
END
$$;

CREATE FUNCTION hub_admin.provision_institution(
    p_actor uuid, p_owner uuid, p_slug text, p_name text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    existing hub_tenancy.institutions%ROWTYPE;
    organization_id uuid;
BEGIN
    IF NOT EXISTS (SELECT FROM hub_identity.users WHERE id = p_actor AND active AND platform_owner) THEN
        RAISE EXCEPTION 'Provisionamento exige o proprietário ativo da plataforma' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT FROM hub_identity.users WHERE id = p_owner AND active) THEN
        RAISE EXCEPTION 'O proprietário da instituição deve ser um usuário ativo';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(p_slug, 164201));
    SELECT * INTO existing FROM hub_tenancy.institutions WHERE slug = p_slug;
    IF FOUND THEN
        IF existing.owner_id <> p_owner OR existing.name IS DISTINCT FROM p_name THEN
            RAISE EXCEPTION 'Slug já utilizado com outro proprietário ou nome' USING ERRCODE = '23505';
        END IF;
        RETURN existing.tenant_id;
    END IF;

    INSERT INTO hub_tenancy.tenants (kind) VALUES ('ORGANIZATION') RETURNING id INTO organization_id;
    INSERT INTO hub_tenancy.memberships (tenant_id, user_id, role)
    VALUES (organization_id, p_owner, 'institution_owner');
    INSERT INTO hub_tenancy.institutions (tenant_id, slug, name, owner_id)
    VALUES (organization_id, p_slug, p_name, p_owner);
    PERFORM hub_admin.create_home(organization_id, p_owner);
    INSERT INTO hub_tenancy.audit_logs (tenant_id, actor_id, action)
    VALUES (organization_id, p_actor, 'institution.provisioned');

    RETURN organization_id;
END
$$;

CREATE FUNCTION hub_admin.add_institution_member(
    p_actor uuid, p_tenant uuid, p_member uuid, p_role text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_role IS NULL OR p_role NOT IN ('institution_admin', 'member', 'viewer') THEN
        RAISE EXCEPTION 'Papel institucional inválido' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT FROM hub_tenancy.institutions AS institution
        JOIN hub_tenancy.tenants AS tenant ON tenant.id = institution.tenant_id
        JOIN hub_tenancy.memberships AS membership ON membership.tenant_id = institution.tenant_id
        JOIN hub_identity.users AS actor ON actor.id = membership.user_id
        WHERE institution.tenant_id = p_tenant AND membership.user_id = p_actor
            AND membership.role = 'institution_owner' AND membership.active
            AND tenant.status = 'ACTIVE' AND actor.active
    ) THEN
        RAISE EXCEPTION 'Operação exige o proprietário ativo da instituição' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT FROM hub_identity.users WHERE id = p_member AND active) THEN
        RAISE EXCEPTION 'O novo membro deve ser um usuário ativo';
    END IF;

    INSERT INTO hub_tenancy.memberships (tenant_id, user_id, role)
    VALUES (p_tenant, p_member, p_role) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        IF EXISTS (
            SELECT FROM hub_tenancy.memberships WHERE tenant_id = p_tenant AND user_id = p_member
                AND role = p_role AND active
        ) THEN
            RETURN;
        END IF;
        RAISE EXCEPTION 'Já existe um vínculo com outro papel ou status' USING ERRCODE = '23505';
    END IF;

    INSERT INTO hub_tenancy.workspace_members (tenant_id, workspace_id, user_id)
    SELECT p_tenant, id, p_member FROM hub_tenancy.workspaces WHERE tenant_id = p_tenant AND slug = 'principal';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Workspace principal ausente';
    END IF;
    INSERT INTO hub_tenancy.audit_logs (tenant_id, actor_id, action)
    VALUES (p_tenant, p_actor, 'institution.member_added');
END
$$;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA hub_admin FROM PUBLIC;
GRANT USAGE ON SCHEMA hub_admin TO hub_operator;
GRANT EXECUTE ON FUNCTION hub_admin.register_identity(text, text, text, boolean),
    hub_admin.provision_institution(uuid, uuid, text, text),
    hub_admin.add_institution_member(uuid, uuid, uuid, text) TO hub_operator;
RESET ROLE;
