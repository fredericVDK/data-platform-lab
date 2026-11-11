-- Test de rechten. Voer uit als admin in psql of DBeaver.

-- Welke gebruikers bestaan er?
SELECT rolname, rolcanlogin, rolsuper FROM pg_roles
WHERE rolname IN ('admin', 'etl_writer', 'bi_reader');

-- Testtabel als etl_writer
SET ROLE etl_writer;
CREATE TABLE dwh.test_rechten (id int, label text);
INSERT INTO dwh.test_rechten VALUES (1, 'werkt');
RESET ROLE;

-- Lezen als bi_reader moet lukken ...
SET ROLE bi_reader;
SELECT * FROM dwh.test_rechten;
-- ... schrijven moet FALEN met "permission denied":
-- INSERT INTO dwh.test_rechten VALUES (2, 'mag niet');
RESET ROLE;

-- Opruimen
DROP TABLE dwh.test_rechten;
