// Sample consumer (fixture): mobile app pet list screen, built against Pet Store API v1.
import { PetStoreClient, Pet } from "./generated/petstore";

const client = new PetStoreClient("https://api.petstore.example.com/v1");

export async function loadPets(page: number): Promise<Row[]> {
  const pets: Pet[] = await client.listPets({ limit: 100 });
  return pets.map((pet) => ({
    title: pet.name.toUpperCase(),
    badge: pet.tag ? `#${pet.tag}` : "",
    statusLabel: statusLabel(pet.status),
  }));
}

function statusLabel(status: Pet["status"]): string {
  switch (status) {
    case "available": return "Ready for adoption";
    case "adopted": return "Found a home";
    case "archived": return "Archived";
  }
  throw new Error(`unknown status ${status}`);
}

interface Row { title: string; badge: string; statusLabel: string }
