const { isConfigured, json, supabaseFetch } = require("./_supabase");

module.exports = async (req, res) => {
  if (req.method !== "GET") {
    return json(res, 405, { error: "Method not allowed" });
  }
  if (!isConfigured()) {
    return json(res, 200, { schedules: {} });
  }

  const response = await supabaseFetch(
    "/rest/v1/schedule_days?select=date_key,schedule_json&order=date_key.asc",
    { method: "GET" }
  );
  if (!response.ok) {
    return json(res, response.status, { error: "Failed to load schedule days" });
  }

  const rows = await response.json();
  const schedules = {};
  for (const row of rows) {
    if (row && row.date_key && row.schedule_json) {
      schedules[row.date_key] = row.schedule_json;
    }
  }
  return json(res, 200, { schedules });
};
