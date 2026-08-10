"use client";

import { useEffect, useRef } from "react";
import { loadGoogleMaps } from "./GoogleRouteMap";

type PlaceResult = {
  formatted_address?: string;
  name?: string;
};
type AutocompleteInstance = {
  addListener(event: "place_changed", handler: () => void): { remove(): void };
  getPlace(): PlaceResult;
};
type PlacesLibrary = {
  Autocomplete: new (
    input: HTMLInputElement,
    options: Record<string, unknown>,
  ) => AutocompleteInstance;
};

export default function GooglePlaceInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const changeHandler = useRef(onChange);

  useEffect(() => {
    changeHandler.current = onChange;
  }, [onChange]);

  useEffect(() => {
    if (!input.current) return;
    let cancelled = false;
    let listener: { remove(): void } | undefined;

    async function connectAutocomplete() {
      try {
        const googleMaps = await loadGoogleMaps();
        const { Autocomplete } = (await googleMaps.importLibrary(
          "places",
        )) as PlacesLibrary;
        if (cancelled || !input.current) return;
        const autocomplete = new Autocomplete(input.current, {
          bounds: {
            north: -37.35,
            south: -38.55,
            east: 145.55,
            west: 144.35,
          },
          componentRestrictions: { country: "au" },
          fields: ["formatted_address", "name"],
          strictBounds: false,
        });
        listener = autocomplete.addListener("place_changed", () => {
          const place = autocomplete.getPlace();
          const selected = place.formatted_address ?? place.name;
          if (selected) changeHandler.current(selected);
        });
      } catch {
        // The input remains fully editable when Places is unavailable.
      }
    }

    void connectAutocomplete();
    return () => {
      cancelled = true;
      listener?.remove();
    };
  }, []);

  return (
    <input
      ref={input}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      autoComplete="off"
      spellCheck={false}
    />
  );
}
