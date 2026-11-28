console.log(
  `worker initialized mode=${process.env.SHARED_MODE || ""} greeting=${process.env.SERVICE_GREETING || ""}`,
);
setTimeout(() => {
  console.log("worker tick");
}, 200);
