// ============================================================
// Cattle Multi-Association Graph — Neo4j Schema
// ============================================================
// Model:
//   (:Animal)        one canonical node per real animal (deduplicated across
//                    associations). uid = the animal's International ID.
//   (:Registration)  one per (association, regNumber); an Animal can have many
//   (:Association)   one per breed registry (Maine-Anjou, Chianina, Shorthorn, Angus...)
//   (:DnaCase)       DNA/genotype case IDs used as fallback merge keys
//   (:Breed)         a breed used in composition (MA, AN, SH, CH, ...)
//   (:Defect)        a genetic defect locus (TH, PHA, CA, AM, NH, ...)
//
// Relationships:
//   (Animal)-[:HAS_REGISTRATION]->(Registration)-[:IN_ASSOCIATION]->(Association)
//   (Animal)-[:HAS_DNA]->(DnaCase)
//   (Animal)-[:HAS_COMPOSITION {percent}]->(Breed)     // breed % breakdown
//   (Animal)-[:TESTED {status}]->(Defect)               // status: Free | Carrier | Suspect | Unknown
//   (sire:Animal)-[:SIRE_OF]->(offspring:Animal)
//   (dam:Animal) -[:DAM_OF]-> (offspring:Animal)
//
// Scalar Animal properties: uid (International ID), name, name_norm, sex, dob,
//   tattoo, color, horn_status ('Horned' | 'Polled' | 'Scurred').
//
// Design notes:
//   * uid IS the International ID — the standardized cross-association identifier,
//     so records for the same animal from different registries collapse on it.
//     (Records lacking an International ID fall back to DNA/registration matching
//     and get a temporary 'TMP-...' uid until an International ID is learned.)
//   * Breed composition and defect status are modeled as edges, not properties,
//     so you can query them: "all animals >= 50% Maine-Anjou", "every TH carrier".
//   * Association-specific facts (EPDs, owner) live on Registration nodes so
//     conflicting values across registries don't clobber each other.
// ============================================================


// ---------- Uniqueness constraints ----------

// Canonical animal id = International ID (or a TMP- id when none is known yet).
CREATE CONSTRAINT animal_uid IF NOT EXISTS
FOR (a:Animal) REQUIRE a.uid IS UNIQUE;

// A registration is unique within an association by its registration number.
CREATE CONSTRAINT registration_key IF NOT EXISTS
FOR (r:Registration) REQUIRE (r.association, r.regNumber) IS UNIQUE;

CREATE CONSTRAINT association_code IF NOT EXISTS
FOR (assoc:Association) REQUIRE assoc.code IS UNIQUE;

CREATE CONSTRAINT dna_case IF NOT EXISTS
FOR (d:DnaCase) REQUIRE d.caseId IS UNIQUE;

CREATE CONSTRAINT breed_code IF NOT EXISTS
FOR (b:Breed) REQUIRE b.code IS UNIQUE;

CREATE CONSTRAINT defect_code IF NOT EXISTS
FOR (x:Defect) REQUIRE x.code IS UNIQUE;


// ---------- Indexes for entity-resolution lookups ----------

CREATE INDEX animal_name   IF NOT EXISTS FOR (a:Animal) ON (a.name);
CREATE INDEX animal_dob    IF NOT EXISTS FOR (a:Animal) ON (a.dob);
// name_norm is what the loader's dedup/stub-merge looks up on — without these,
// every such lookup scans ALL animals (the main write-speed killer as the graph grows).
CREATE INDEX animal_name_norm IF NOT EXISTS FOR (a:Animal) ON (a.name_norm);
CREATE INDEX animal_namenorm_dob IF NOT EXISTS FOR (a:Animal) ON (a.name_norm, a.dob);
CREATE INDEX animal_tattoo IF NOT EXISTS FOR (a:Animal) ON (a.tattoo);
CREATE INDEX animal_color  IF NOT EXISTS FOR (a:Animal) ON (a.color);
CREATE INDEX animal_horn   IF NOT EXISTS FOR (a:Animal) ON (a.horn_status);
CREATE INDEX registration_number IF NOT EXISTS FOR (r:Registration) ON (r.regNumber);


// ---------- Seed reference nodes ----------

MERGE (a:Association {code: 'MAINE'})
  SET a.name = 'American Maine-Anjou Association', a.platform = 'DigitalBeef', a.subdomain = 'maine-anjou';
MERGE (a:Association {code: 'CHIA'})
  SET a.name = 'American Chianina Association', a.platform = 'DigitalBeef', a.subdomain = 'chianina';
MERGE (a:Association {code: 'SHORT'})
  SET a.name = 'American Shorthorn Association', a.platform = 'DigitalBeef', a.subdomain = 'shorthorn';
MERGE (a:Association {code: 'ANGUS'})
  SET a.name = 'American Angus Association', a.platform = 'Custom (angus.org)', a.subdomain = 'www.angus';

MERGE (b:Breed {code:'MA'}) SET b.name = 'Maine-Anjou';
MERGE (b:Breed {code:'AN'}) SET b.name = 'Angus';
MERGE (b:Breed {code:'SH'}) SET b.name = 'Shorthorn';
MERGE (b:Breed {code:'CH'}) SET b.name = 'Chianina';

// Common beef genetic-defect loci (extend as needed).
MERGE (x:Defect {code:'TH'})   SET x.name = 'Tibial Hemimelia';
MERGE (x:Defect {code:'PHA'})  SET x.name = 'Pulmonary Hypoplasia with Anasarca';
MERGE (x:Defect {code:'CA'})   SET x.name = 'Contractural Arachnodactyly (fawn calf)';
MERGE (x:Defect {code:'MSUD'}) SET x.name = 'Maple Syrup Urine Disease';
MERGE (x:Defect {code:'AM'})   SET x.name = 'Arthrogryposis Multiplex (curly calf)';
MERGE (x:Defect {code:'NH'})   SET x.name = 'Neuropathic Hydrocephalus';
MERGE (x:Defect {code:'DD'})   SET x.name = 'Developmental Duplication';
