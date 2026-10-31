CREATE SCHEMA IF NOT EXISTS commerce AUTHORIZATION codestra_whatsapp_owner;
SET LOCAL ROLE codestra_whatsapp_owner;
CREATE TABLE commerce.businesses (
	name VARCHAR(200) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	timezone VARCHAR(100) NOT NULL,
	active BOOLEAN NOT NULL,
	tax_bps INTEGER NOT NULL,
	settings JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	CHECK (currency IN ('USD','DOP')),
	CHECK (tax_bps BETWEEN 0 AND 10000)
);

CREATE TABLE commerce.memberships (
	subject VARCHAR(255) NOT NULL,
	role VARCHAR(30) NOT NULL,
	active BOOLEAN NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, subject),
	CHECK (role IN ('owner','supervisor','agent','fulfillment')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_memberships_tenant_id ON commerce.memberships (tenant_id);

CREATE TABLE commerce.channels (
	provider VARCHAR(30) NOT NULL,
	account_id VARCHAR(200) NOT NULL,
	phone_number_id VARCHAR(200) NOT NULL,
	active BOOLEAN NOT NULL,
	verification_reference VARCHAR(255),
	policy_campaign_id VARCHAR(200),
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (provider, account_id, phone_number_id),
	CHECK (provider IN ('meta','evolution')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_channels_tenant_id ON commerce.channels (tenant_id);

CREATE TABLE commerce.products (
	sku VARCHAR(100) NOT NULL,
	name VARCHAR(200) NOT NULL,
	description TEXT NOT NULL,
	price_minor BIGINT NOT NULL,
	currency VARCHAR(3) NOT NULL,
	stock_on_hand INTEGER NOT NULL,
	stock_reserved INTEGER NOT NULL,
	version INTEGER NOT NULL,
	active BOOLEAN NOT NULL,
	image_url TEXT,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, sku),
	CHECK (price_minor >= 0),
	CHECK (stock_on_hand >= 0 AND stock_reserved >= 0 AND stock_reserved <= stock_on_hand),
	CHECK (currency IN ('USD','DOP')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_products_tenant_id ON commerce.products (tenant_id);

CREATE TABLE commerce.delivery_zones (
	code VARCHAR(100) NOT NULL,
	name VARCHAR(200) NOT NULL,
	fee_minor BIGINT NOT NULL,
	minimum_minor BIGINT NOT NULL,
	active BOOLEAN NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code),
	CHECK (fee_minor >= 0 AND minimum_minor >= 0),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_delivery_zones_tenant_id ON commerce.delivery_zones (tenant_id);

CREATE TABLE commerce.audit_events (
	actor VARCHAR(255) NOT NULL,
	action VARCHAR(100) NOT NULL,
	object_type VARCHAR(100) NOT NULL,
	object_id UUID,
	details JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_audit_events_tenant_id ON commerce.audit_events (tenant_id);

CREATE TABLE commerce.conversations (
	channel_id UUID NOT NULL,
	customer_ref VARCHAR(255) NOT NULL,
	language VARCHAR(2) NOT NULL,
	mode VARCHAR(10) NOT NULL,
	assigned_to VARCHAR(255),
	version INTEGER NOT NULL,
	consent_status VARCHAR(20) NOT NULL,
	consent_at TIMESTAMP WITH TIME ZONE,
	consent_evidence TEXT,
	bot_state JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, channel_id) REFERENCES commerce.channels (tenant_id, id),
	UNIQUE (tenant_id, channel_id, customer_ref),
	CHECK (language IN ('en','es')),
	CHECK (mode IN ('bot','human')),
	CHECK (consent_status IN ('unknown','opted_in','opted_out')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_conversations_tenant_id ON commerce.conversations (tenant_id);

CREATE TABLE commerce.inbound_events (
	channel_id UUID NOT NULL,
	event_id VARCHAR(200) NOT NULL,
	payload_hash VARCHAR(64) NOT NULL,
	payload JSONB NOT NULL,
	state VARCHAR(20) NOT NULL,
	attempts INTEGER NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_error VARCHAR(100),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, channel_id) REFERENCES commerce.channels (tenant_id, id),
	UNIQUE (tenant_id, channel_id, event_id),
	CHECK (state IN ('pending','done','dead')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_inbound_events_tenant_id ON commerce.inbound_events (tenant_id);

CREATE TABLE commerce.messages (
	conversation_id UUID NOT NULL,
	provider_message_id VARCHAR(255),
	direction VARCHAR(20) NOT NULL,
	text TEXT NOT NULL,
	status VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, conversation_id) REFERENCES commerce.conversations (tenant_id, id),
	UNIQUE (tenant_id, conversation_id, provider_message_id),
	CHECK (direction IN ('inbound','outbound')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_messages_tenant_id ON commerce.messages (tenant_id);

CREATE TABLE commerce.carts (
	conversation_id UUID NOT NULL,
	version INTEGER NOT NULL,
	status VARCHAR(20) NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, conversation_id) REFERENCES commerce.conversations (tenant_id, id),
	CHECK (status IN ('active','ordered')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_carts_tenant_id ON commerce.carts (tenant_id);

CREATE UNIQUE INDEX uq_active_cart ON commerce.carts (tenant_id, conversation_id) WHERE status = 'active';

CREATE TABLE commerce.outbox_events (
	conversation_id UUID NOT NULL,
	conversation_version INTEGER NOT NULL,
	event_key VARCHAR(255) NOT NULL,
	kind VARCHAR(100) NOT NULL,
	payload JSONB NOT NULL,
	state VARCHAR(20) NOT NULL,
	attempts INTEGER NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_error VARCHAR(100),
	command_id UUID NOT NULL,
	policy_operation_id VARCHAR(255),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, conversation_id) REFERENCES commerce.conversations (tenant_id, id),
	UNIQUE (tenant_id, event_key),
	CHECK (state IN ('pending','done','sent','cancelled','dead')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_outbox_events_tenant_id ON commerce.outbox_events (tenant_id);

CREATE TABLE commerce.cart_items (
	cart_id UUID NOT NULL,
	product_id UUID NOT NULL,
	quantity INTEGER NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, cart_id) REFERENCES commerce.carts (tenant_id, id),
	FOREIGN KEY(tenant_id, product_id) REFERENCES commerce.products (tenant_id, id),
	UNIQUE (tenant_id, cart_id, product_id),
	CHECK (quantity BETWEEN 1 AND 100),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_cart_items_tenant_id ON commerce.cart_items (tenant_id);

CREATE TABLE commerce.quotes (
	cart_id UUID NOT NULL,
	conversation_id UUID NOT NULL,
	cart_version INTEGER NOT NULL,
	currency VARCHAR(3) NOT NULL,
	subtotal_minor BIGINT NOT NULL,
	delivery_minor BIGINT NOT NULL,
	tax_minor BIGINT NOT NULL,
	total_minor BIGINT NOT NULL,
	address TEXT NOT NULL,
	fulfillment VARCHAR(20) NOT NULL,
	zone_code VARCHAR(100),
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, cart_id) REFERENCES commerce.carts (tenant_id, id),
	FOREIGN KEY(tenant_id, conversation_id) REFERENCES commerce.conversations (tenant_id, id),
	CHECK (fulfillment IN ('pickup','delivery')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_quotes_tenant_id ON commerce.quotes (tenant_id);

CREATE TABLE commerce.quote_items (
	quote_id UUID NOT NULL,
	product_id UUID NOT NULL,
	product_version INTEGER NOT NULL,
	sku VARCHAR(100) NOT NULL,
	name VARCHAR(200) NOT NULL,
	quantity INTEGER NOT NULL,
	unit_price_minor BIGINT NOT NULL,
	subtotal_minor BIGINT NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, quote_id) REFERENCES commerce.quotes (tenant_id, id),
	FOREIGN KEY(tenant_id, product_id) REFERENCES commerce.products (tenant_id, id),
	UNIQUE (tenant_id, quote_id, product_id),
	CHECK (quantity BETWEEN 1 AND 100),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_quote_items_tenant_id ON commerce.quote_items (tenant_id);

CREATE TABLE commerce.orders (
	quote_id UUID NOT NULL,
	conversation_id UUID NOT NULL,
	idempotency_key VARCHAR(200) NOT NULL,
	status VARCHAR(30) NOT NULL,
	payment_status VARCHAR(30) NOT NULL,
	currency VARCHAR(3) NOT NULL,
	subtotal_minor BIGINT NOT NULL,
	delivery_minor BIGINT NOT NULL,
	tax_minor BIGINT NOT NULL,
	total_minor BIGINT NOT NULL,
	address TEXT NOT NULL,
	fulfillment VARCHAR(20) NOT NULL,
	reservation_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, quote_id) REFERENCES commerce.quotes (tenant_id, id),
	FOREIGN KEY(tenant_id, conversation_id) REFERENCES commerce.conversations (tenant_id, id),
	UNIQUE (tenant_id, quote_id),
	UNIQUE (tenant_id, idempotency_key),
	CHECK (status IN ('submitted','accepted','preparing','out_for_delivery','delivered','ready_for_pickup','collected','rejected','cancelled')),
	CHECK (payment_status IN ('unpaid','paid_externally','refunded_externally')),
	CHECK (fulfillment IN ('pickup','delivery')),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_orders_tenant_id ON commerce.orders (tenant_id);

CREATE TABLE commerce.order_items (
	order_id UUID NOT NULL,
	product_id UUID NOT NULL,
	sku VARCHAR(100) NOT NULL,
	name VARCHAR(200) NOT NULL,
	quantity INTEGER NOT NULL,
	unit_price_minor BIGINT NOT NULL,
	subtotal_minor BIGINT NOT NULL,
	tenant_id UUID NOT NULL,
	id UUID NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, order_id) REFERENCES commerce.orders (tenant_id, id),
	FOREIGN KEY(tenant_id, product_id) REFERENCES commerce.products (tenant_id, id),
	UNIQUE (tenant_id, order_id, product_id),
	CHECK (quantity BETWEEN 1 AND 100),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES commerce.businesses (id)
);

CREATE INDEX ix_commerce_order_items_tenant_id ON commerce.order_items (tenant_id);
ALTER TABLE commerce.businesses ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.businesses FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.businesses TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.audit_events TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.channels FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.channels TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.delivery_zones ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.delivery_zones FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.delivery_zones TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.memberships TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.products ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.products FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.products TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.conversations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.conversations TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.inbound_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.inbound_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.inbound_events TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.carts ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.carts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.carts TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.messages FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.messages TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.outbox_events TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.cart_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.cart_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.cart_items TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.quotes ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.quotes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.quotes TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.orders FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.orders TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.quote_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.quote_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.quote_items TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE commerce.order_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE commerce.order_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON commerce.order_items TO codestra_whatsapp_app, codestra_whatsapp_owner
USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY route_owner_channels ON commerce.channels FOR SELECT TO codestra_whatsapp_owner USING (true);
CREATE POLICY route_owner_businesses ON commerce.businesses FOR SELECT TO codestra_whatsapp_owner USING (true);
CREATE FUNCTION commerce.lookup_channel(p_provider text, p_account text, p_phone text)
RETURNS TABLE(tenant_id uuid, channel_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, commerce
AS $$ SELECT c.tenant_id,c.id FROM commerce.channels c JOIN commerce.businesses b ON b.id=c.tenant_id
WHERE c.provider=p_provider AND c.account_id=p_account AND c.phone_number_id=p_phone
AND c.active AND b.active AND c.verification_reference IS NOT NULL AND length(c.verification_reference)>0 $$;
REVOKE ALL ON FUNCTION commerce.lookup_channel(text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION commerce.lookup_channel(text,text,text) TO codestra_whatsapp_app;
GRANT USAGE ON SCHEMA commerce TO codestra_whatsapp_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA commerce TO codestra_whatsapp_app;
REVOKE UPDATE,DELETE ON commerce.audit_events FROM codestra_whatsapp_app;
REVOKE UPDATE,DELETE ON commerce.quote_items,commerce.order_items FROM codestra_whatsapp_app;
REVOKE INSERT,UPDATE,DELETE ON commerce.channels FROM codestra_whatsapp_app;
GRANT UPDATE(active) ON commerce.channels TO codestra_whatsapp_app;
CREATE POLICY channel_disable_only ON commerce.channels AS RESTRICTIVE FOR UPDATE TO codestra_whatsapp_app USING (true) WITH CHECK (active = false);
RESET ROLE;
