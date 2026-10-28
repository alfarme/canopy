import { useQuery } from "@tanstack/react-query";
import { fetchPlant, fetchPlants } from "./api";

export function usePlants(refetchIntervalMs?: number) {
  return useQuery({
    queryKey: ["plants"],
    queryFn: fetchPlants,
    refetchInterval: refetchIntervalMs,
  });
}

export function usePlant(id: string) {
  return useQuery({ queryKey: ["plant", id], queryFn: () => fetchPlant(id) });
}
