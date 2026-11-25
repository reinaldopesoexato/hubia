CREATE ROLE hub_portal LOGIN INHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOBYPASSRLS;
GRANT hub_runtime TO hub_portal;
CREATE SCHEMA hub_accounts AUTHORIZATION hub_portal;

SET LOCAL ROLE hub_provisioning_code;
CREATE FUNCTION hub_admin.portal_principal(p_subject text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT u.id FROM hub_identity.identities AS i
    JOIN hub_identity.users AS u ON u.id = i.user_id
    WHERE i.issuer = 'urn:hubia:portal:local' AND i.subject = p_subject AND u.active
$$;
REVOKE ALL ON FUNCTION hub_admin.portal_principal(text) FROM PUBLIC;
GRANT USAGE ON SCHEMA hub_admin TO hub_portal;
GRANT EXECUTE ON FUNCTION hub_admin.portal_principal(text) TO hub_portal;
RESET ROLE;

SET LOCAL ROLE hub_portal;
CREATE TABLE hub_accounts.login_limits (
    source text PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 1
);
CREATE INDEX login_limits_expiry ON hub_accounts.login_limits (started_at);
RESET ROLE;
