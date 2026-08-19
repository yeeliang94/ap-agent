import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// The suite runs without vitest's globals, so Testing Library's automatic
// cleanup (which hooks a global afterEach) never registers itself. Do it
// here, once, or every render piles up in the same document and a second
// `getByRole` finds two of everything.
afterEach(cleanup);
