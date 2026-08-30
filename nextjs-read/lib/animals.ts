import "server-only";
import { read } from "./neo4j";

/* ---------------------------------------------------------------------------
 * Types — mirror the graph schema (see schema.cypher).
 * ------------------------------------------------------------------------- */

export interface Registration {
  association: string;
  regNumber: string;
  owner?: string | null;
  breeder?: string | null;
  epds: Record<string, number>;          // parsed from JSON string on the node
  attributes: Record<string, unknown>;   // the free-form "everything else" bag
  sourceUrl?: string | null;
  scrapedAt?: string | null;
}

export interface AnimalSummary {
  uid: string;              // International ID (canonical id)
  intlId?: string | null;
  name?: string | null;
  sex?: string | null;
  dob?: string | null;
  tattoo?: string | null;
  color?: string | null;
  hornStatus?: string | null;
}

export interface BreedShare {
  breed: string;
  percent: number;
}

export interface DefectResult {
  code: string;
  status: "Free" | "Carrier" | "Suspect" | "Unknown" | string;
}

export interface AnimalDetail extends AnimalSummary {
  registrations: Registration[];
  breedComposition: BreedShare[];
  defects: DefectResult[];
  sire?: AnimalSummary | null;
  dam?: AnimalSummary | null;
}

/* ---------------------------------------------------------------------------
 * Mapping helpers
 * ------------------------------------------------------------------------- */

function parseJson<T>(value: unknown, fallback: T): T {
  if (typeof value !== "string" || value.length === 0) return fallback;
  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
}

interface RawAnimal {
  uid: string;
  intl_id?: string | null;
  name?: string | null;
  sex?: string | null;
  dob?: string | null;
  tattoo?: string | null;
  color?: string | null;
  horn_status?: string | null;
}

function mapAnimal(a: RawAnimal | null | undefined): AnimalSummary | null {
  if (!a || !a.uid) return null;
  return {
    uid: a.uid,
    intlId: a.intl_id ?? null,
    name: a.name ?? null,
    sex: a.sex ?? null,
    dob: a.dob ?? null,
    tattoo: a.tattoo ?? null,
    color: a.color ?? null,
    hornStatus: a.horn_status ?? null,
  };
}

interface RawRegistration {
  association: string;
  regNumber: string;
  owner?: string | null;
  breeder?: string | null;
  epds?: string | null;
  attributes?: string | null;
  source_url?: string | null;
  scraped_at?: string | null;
  stub?: boolean;
}

function mapRegistration(r: RawRegistration): Registration {
  return {
    association: r.association,
    regNumber: r.regNumber,
    owner: r.owner ?? null,
    breeder: r.breeder ?? null,
    epds: parseJson<Record<string, number>>(r.epds, {}),
    attributes: parseJson<Record<string, unknown>>(r.attributes, {}),
    sourceUrl: r.source_url ?? null,
    scrapedAt: r.scraped_at ?? null,
  };
}

/* ---------------------------------------------------------------------------
 * Query functions — this is the API your app calls.
 * ------------------------------------------------------------------------- */

/** Full profile for one animal, keyed by International ID (uid). */
export async function getAnimal(uid: string): Promise<AnimalDetail | null> {
  const rows = await read<{
    animal: RawAnimal;
    registrations: RawRegistration[];
    breedComposition: Array<{ breed: string | null; percent: number | null }>;
    defects: Array<{ code: string | null; status: string | null }>;
    sire: RawAnimal | null;
    dam: RawAnimal | null;
  }>(
    `
    MATCH (a:Animal {uid: $uid})
    OPTIONAL MATCH (a)-[:HAS_REGISTRATION]->(r:Registration)
    OPTIONAL MATCH (a)-[c:HAS_COMPOSITION]->(b:Breed)
    OPTIONAL MATCH (a)-[t:TESTED]->(x:Defect)
    OPTIONAL MATCH (sire:Animal)-[:SIRE_OF]->(a)
    OPTIONAL MATCH (dam:Animal)-[:DAM_OF]->(a)
    RETURN a AS animal,
           collect(DISTINCT r {.*})                              AS registrations,
           collect(DISTINCT {breed: b.code, percent: c.percent}) AS breedComposition,
           collect(DISTINCT {code: x.code, status: t.status})    AS defects,
           head(collect(DISTINCT sire {.*}))                     AS sire,
           head(collect(DISTINCT dam {.*}))                      AS dam
    `,
    { uid }
  );

  if (rows.length === 0) return null;
  const row = rows[0];
  const base = mapAnimal(row.animal);
  if (!base) return null;

  return {
    ...base,
    registrations: (row.registrations ?? [])
      .filter((r) => r && r.association && !r.stub)
      .map(mapRegistration),
    breedComposition: (row.breedComposition ?? [])
      .filter((c) => c.breed != null)
      .map((c) => ({ breed: c.breed as string, percent: Number(c.percent ?? 0) })),
    defects: (row.defects ?? [])
      .filter((d) => d.code != null)
      .map((d) => ({ code: d.code as string, status: (d.status as string) ?? "" })),
    sire: mapAnimal(row.sire),
    dam: mapAnimal(row.dam),
  };
}

/** Animals registered in more than one association. */
export async function listMultiAssociationAnimals(
  limit = 100
): Promise<Array<AnimalSummary & { associations: string[] }>> {
  const rows = await read<{ animal: RawAnimal; associations: string[] }>(
    `
    MATCH (a:Animal)-[:HAS_REGISTRATION]->(r:Registration)
    WITH a, collect(DISTINCT r.association) AS associations
    WHERE size(associations) > 1
    RETURN a AS animal, associations
    ORDER BY a.name
    LIMIT $limit
    `,
    { limit }
  );
  return rows.map((row) => ({
    ...(mapAnimal(row.animal) as AnimalSummary),
    associations: row.associations,
  }));
}

/** Every carrier of a given genetic defect. */
export async function carriersOfDefect(
  defectCode: string,
  limit = 200
): Promise<AnimalSummary[]> {
  const rows = await read<{ animal: RawAnimal }>(
    `
    MATCH (a:Animal)-[t:TESTED {status: 'Carrier'}]->(:Defect {code: $code})
    RETURN a AS animal
    ORDER BY a.name
    LIMIT $limit
    `,
    { code: defectCode.toUpperCase(), limit }
  );
  return rows.map((row) => mapAnimal(row.animal) as AnimalSummary);
}

/** Animals whose composition is at least `minPercent` of a breed. */
export async function animalsByBreed(
  breedCode: string,
  minPercent = 50,
  limit = 200
): Promise<Array<AnimalSummary & { percent: number }>> {
  const rows = await read<{ animal: RawAnimal; percent: number }>(
    `
    MATCH (a:Animal)-[c:HAS_COMPOSITION]->(:Breed {code: $code})
    WHERE c.percent >= $minPercent
    RETURN a AS animal, c.percent AS percent
    ORDER BY c.percent DESC, a.name
    LIMIT $limit
    `,
    { code: breedCode.toUpperCase(), minPercent, limit }
  );
  return rows.map((row) => ({
    ...(mapAnimal(row.animal) as AnimalSummary),
    percent: Number(row.percent),
  }));
}

/** Ancestors up to `generations` levels above an animal (pedigree). */
export async function getPedigree(
  uid: string,
  generations = 5
): Promise<Array<{ ancestor: AnimalSummary; depth: number; role: string }>> {
  const rows = await read<{ ancestor: RawAnimal; depth: number; role: string }>(
    `
    MATCH path = (a:Animal {uid: $uid})<-[rels:SIRE_OF|DAM_OF*1..${Math.max(1, Math.min(generations, 20))}]-(anc:Animal)
    RETURN anc AS ancestor,
           length(path) AS depth,
           type(last(rels)) AS role
    ORDER BY depth
    `,
    { uid }
  );
  return rows.map((row) => ({
    ancestor: mapAnimal(row.ancestor) as AnimalSummary,
    depth: Number(row.depth),
    role: row.role === "SIRE_OF" ? "sire" : "dam",
  }));
}
