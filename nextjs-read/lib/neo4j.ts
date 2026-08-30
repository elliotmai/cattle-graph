import "server-only";
import neo4j, { Driver } from "neo4j-driver";

/**
 * HMR-safe Neo4j driver singleton.
 *
 * Next.js dev mode reloads modules on every edit; without caching on
 * globalThis you'd leak a new driver (and its connection pool) each time.
 * The driver is a heavyweight, long-lived object — create one, reuse it.
 */
const globalForNeo4j = globalThis as unknown as { neo4jDriver?: Driver };

export function getDriver(): Driver {
  if (!globalForNeo4j.neo4jDriver) {
    const uri = process.env.NEO4J_URI;
    const user = process.env.NEO4J_USER;
    const password = process.env.NEO4J_PASSWORD;
    if (!uri || !user || !password) {
      throw new Error(
        "Missing NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD env vars (see .env.example)."
      );
    }
    globalForNeo4j.neo4jDriver = neo4j.driver(uri, neo4j.auth.basic(user, password), {
      // Return plain JS numbers instead of Neo4j Integer objects — fine for
      // this data (no values exceed 2^53) and much nicer to consume.
      disableLosslessIntegers: true,
    });
  }
  return globalForNeo4j.neo4jDriver;
}

/**
 * Run a read-only Cypher query and return typed rows.
 * READ access mode makes intent explicit and lets a clustered/Aura setup
 * route the query to a read replica.
 */
export async function read<T = Record<string, unknown>>(
  cypher: string,
  params: Record<string, unknown> = {}
): Promise<T[]> {
  const session = getDriver().session({ defaultAccessMode: neo4j.session.READ });
  try {
    const result = await session.run(cypher, params);
    return result.records.map((r) => r.toObject() as T);
  } finally {
    await session.close();
  }
}
