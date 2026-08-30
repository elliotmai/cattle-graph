import { NextResponse } from "next/server";
import { getAnimal } from "@/lib/animals";

// GET /api/animals/USAM20180211Z001
// In Next.js 15 route handlers, `params` is a Promise and must be awaited.
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ uid: string }> }
) {
  const { uid } = await params;
  const animal = await getAnimal(uid);

  if (!animal) {
    return NextResponse.json({ error: "Animal not found" }, { status: 404 });
  }
  return NextResponse.json(animal);
}
