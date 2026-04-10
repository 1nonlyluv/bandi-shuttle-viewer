const { isConfigured, json, readJsonBody, supabaseFetch } = require("./_supabase");

function validDateKey(value) {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
}

module.exports = async (req, res) => {
  if (!isConfigured()) {
    return json(res, 503, { error: "Supabase is not configured" });
  }

  if (req.method === "GET") {
    const dateKey = req.query?.date;
    if (!validDateKey(dateKey)) {
      return json(res, 400, { error: "A valid date query is required" });
    }
    const response = await supabaseFetch(
      `/rest/v1/schedule_overrides?date_key=eq.${encodeURIComponent(dateKey)}&override_key=eq.full_schedule&select=payload,updated_at&limit=1`,
      { method: "GET" }
    );
    if (!response.ok) {
      return json(res, response.status, { error: "Failed to load shared override" });
    }
    const rows = await response.json();
    if (!rows.length) {
      return json(res, 404, { error: "No shared override found" });
    }
    return json(res, 200, { data: rows[0].payload, updated_at: rows[0].updated_at });
  }

  if (req.method === "POST") {
    const body = await readJsonBody(req);
    if (!validDateKey(body.date_key) || !body.data || typeof body.data !== "object") {
      return json(res, 400, { error: "date_key and data are required" });
    }
    const response = await supabaseFetch("/rest/v1/schedule_overrides?on_conflict=date_key,override_key", {
      method: "POST",
      headers: {
        Prefer: "resolution=merge-duplicates,return=representation",
      },
      body: JSON.stringify([
        {
          date_key: body.date_key,
          override_key: "full_schedule",
          payload: body.data,
          updated_by: body.updated_by || null,
        },
      ]),
    });
    if (!response.ok) {
      return json(res, response.status, { error: "Failed to save shared override" });
    }
    const rows = await response.json();
    return json(res, 200, { ok: true, row: rows[0] || null });
  }

  if (req.method === "DELETE") {
    const dateKey = req.query?.date;
    if (!validDateKey(dateKey)) {
      return json(res, 400, { error: "A valid date query is required" });
    }
    const response = await supabaseFetch(
      `/rest/v1/schedule_overrides?date_key=eq.${encodeURIComponent(dateKey)}&override_key=eq.full_schedule`,
      {
        method: "DELETE",
        headers: {
          Prefer: "return=representation",
        },
      }
    );
    if (!response.ok) {
      return json(res, response.status, { error: "Failed to clear shared override" });
    }
    return json(res, 200, { ok: true });
  }

  return json(res, 405, { error: "Method not allowed" });
};
