// Firefox user.js — performance-tuned for minimal Openbox desktop
// Intel integrated graphics optimized

// Hardware acceleration
user_pref("gfx.webrender.all", true);
user_pref("media.ffmpeg.vaapi.enabled", true);
user_pref("media.hardware-video-decoding.enabled", true);
user_pref("layers.acceleration.force-enabled", true);

// Reduce animations and visual overhead
user_pref("toolkit.cosmeticAnimations.enabled", false);
user_pref("ui.prefersReducedMotion", 1);
user_pref("browser.fullscreen.animateUp", 0);

// Memory and performance
user_pref("browser.cache.disk.capacity", 256000);
user_pref("browser.sessionhistory.max_entries", 15);
user_pref("browser.sessionstore.interval", 30000);
user_pref("browser.tabs.unloadOnLowMemory", true);

// Network tuning
user_pref("network.http.max-persistent-connections-per-server", 8);
user_pref("network.http.pipelining", true);
user_pref("network.dns.disablePrefetch", false);
user_pref("network.prefetch-next", true);

// Rendering performance
user_pref("nglayout.initialpaint.delay", 0);
user_pref("content.notify.interval", 100000);

// Disable telemetry and background noise
user_pref("toolkit.telemetry.enabled", false);
user_pref("toolkit.telemetry.unified", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.ping-centre.telemetry", false);
user_pref("browser.newtabpage.activity-stream.feeds.telemetry", false);
user_pref("browser.newtabpage.activity-stream.telemetry", false);

// Disable Pocket, Snippets, sponsored content
user_pref("extensions.pocket.enabled", false);
user_pref("browser.newtabpage.activity-stream.feeds.snippets", false);
user_pref("browser.newtabpage.activity-stream.showSponsored", false);
user_pref("browser.newtabpage.activity-stream.showSponsoredTopSites", false);

// Cleaner new tab
user_pref("browser.newtabpage.activity-stream.feeds.topsites", false);
user_pref("browser.newtabpage.activity-stream.feeds.section.highlights", false);

// Dark theme preference (matches Openbox dark setup)
user_pref("ui.systemUsesDarkTheme", 1);
user_pref("browser.theme.content-theme", 0);
user_pref("browser.theme.toolbar-theme", 0);

// Compact density for minimal UI
user_pref("browser.compactmode.show", true);
user_pref("browser.uidensity", 1);

// Disable update nag (snap handles updates)
user_pref("app.update.enabled", false);
user_pref("app.update.auto", false);
