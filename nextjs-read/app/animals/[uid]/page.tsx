import { getAnimal } from "@/lib/animals";
import { notFound } from "next/navigation";

// Server Component — runs on the server, queries Neo4j directly, no API round-trip.
// In Next.js 15, `params` is a Promise.
export default async function AnimalPage({
  params,
}: {
  params: Promise<{ uid: string }>;
}) {
  const { uid } = await params;
  const animal = await getAnimal(uid);
  if (!animal) notFound();

  return (
    <main style={{ maxWidth: 720, margin: "2rem auto", fontFamily: "system-ui" }}>
      <h1>{animal.name ?? "(unnamed)"}</h1>
      <p>
        <strong>International ID:</strong> {animal.uid}
        {" · "}
        <strong>Sex:</strong> {animal.sex ?? "?"}
        {" · "}
        <strong>DOB:</strong> {animal.dob ?? "?"}
      </p>
      <p>
        <strong>Color:</strong> {animal.color ?? "?"}
        {" · "}
        <strong>Horn:</strong> {animal.hornStatus ?? "?"}
      </p>

      <h2>Registrations</h2>
      <ul>
        {animal.registrations.map((r) => (
          <li key={`${r.association}:${r.regNumber}`}>
            {r.association} {r.regNumber}
            {r.owner ? ` — ${r.owner}` : ""}
          </li>
        ))}
      </ul>

      <h2>Breed composition</h2>
      <ul>
        {animal.breedComposition.map((c) => (
          <li key={c.breed}>
            {c.breed}: {c.percent}%
          </li>
        ))}
      </ul>

      <h2>Genetic defects</h2>
      <ul>
        {animal.defects.map((d) => (
          <li key={d.code}>
            {d.code}: {d.status}
          </li>
        ))}
      </ul>

      <h2>Parents</h2>
      <p>Sire: {animal.sire?.name ?? "—"}</p>
      <p>Dam: {animal.dam?.name ?? "—"}</p>
    </main>
  );
}
