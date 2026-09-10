-- Runs only on first Postgres init (empty data volume). Creates a separate
-- database for Langfuse's own schema so it never collides with da-voice's own
-- calls/turns/events/slots tables in the da_voice database. Same Postgres
-- container/service — this does not add a service.
CREATE DATABASE langfuse;
