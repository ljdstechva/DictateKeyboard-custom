/*
 * Custom build only: self-update from the private GitHub repository's releases.
 *
 * The repo, the tag this APK was built from and a read-only token are injected by CI into
 * BuildConfig. When CUSTOM_UPDATE_REPO is blank (any non-CI or upstream build) the updater is
 * disabled and none of its UI is shown. Android never installs an APK silently: the last step
 * hands the downloaded file to the system package installer, which asks the user to confirm.
 */
package dev.patrickgold.florisboard.app.custom

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import dev.patrickgold.florisboard.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

data class CustomRelease(
    val tag: String,
    val title: String,
    val publishedAt: String,
    val notes: String,
    val assetName: String,
    val assetApiUrl: String,
    val assetSize: Long,
    val htmlUrl: String,
)

object CustomUpdater {
    val REPO: String = BuildConfig.CUSTOM_UPDATE_REPO
    val BUILT_TAG: String = BuildConfig.CUSTOM_RELEASE_TAG
    private val TOKEN: String = BuildConfig.CUSTOM_UPDATE_TOKEN
    private const val CACHE_DIR = "updates"

    /** Minimum gap between automatic checks (6 hours). */
    const val AUTO_CHECK_INTERVAL_MS: Long = 6L * 60L * 60L * 1000L

    val enabled: Boolean get() = REPO.isNotBlank()
    val hasToken: Boolean get() = TOKEN.isNotBlank()

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build()
    }

    private fun Request.Builder.github(accept: String): Request.Builder {
        header("Accept", accept)
        header("X-GitHub-Api-Version", "2022-11-28")
        header("User-Agent", "DictateCustomUpdater/${BuildConfig.VERSION_NAME}")
        if (TOKEN.isNotBlank()) header("Authorization", "Bearer $TOKEN")
        return this
    }

    class UpdateException(message: String) : IOException(message)

    /**
     * Newest non-draft release that carries an .apk asset, or null when the repo has none.
     * GitHub lists releases newest first; pre-releases are included on purpose (CI publishes
     * every build as a pre-release).
     */
    suspend fun fetchLatest(): CustomRelease? = withContext(Dispatchers.IO) {
        if (!enabled) throw UpdateException("Updater is not configured in this build.")
        val request = Request.Builder()
            .url("https://api.github.com/repos/$REPO/releases?per_page=10")
            .github("application/vnd.github+json")
            .build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw UpdateException(httpError(response.code))
            val body = response.body.string()
            val arr = JSONArray(body)
            for (i in 0 until arr.length()) {
                val rel = arr.getJSONObject(i)
                if (rel.optBoolean("draft", false)) continue
                val assets = rel.optJSONArray("assets") ?: continue
                for (j in 0 until assets.length()) {
                    val asset = assets.getJSONObject(j)
                    val name = asset.optString("name")
                    if (!name.endsWith(".apk", ignoreCase = true)) continue
                    return@withContext CustomRelease(
                        tag = rel.optString("tag_name"),
                        title = rel.optString("name").ifBlank { rel.optString("tag_name") },
                        publishedAt = rel.optString("published_at"),
                        notes = rel.optString("body"),
                        assetName = name,
                        assetApiUrl = asset.optString("url"),
                        assetSize = asset.optLong("size", -1L),
                        htmlUrl = rel.optString("html_url"),
                    )
                }
            }
            null
        }
    }

    private fun httpError(code: Int): String = when (code) {
        401 -> "GitHub rejected the update token (401). It may have expired: create a new one and rebuild."
        403 -> "GitHub refused the request (403): rate limit or token without access to the repository."
        404 -> if (hasToken) {
            "Repository not found (404). The token may not have access to $REPO."
        } else {
            "Repository not found (404). This build has no update token, and the repository is private."
        }
        else -> "GitHub returned HTTP $code."
    }

    /** Parses `v6.4.0-custom.12` into comparable numbers; null for anything else. */
    fun parseTag(tag: String): List<Int>? {
        val m = TAG_REGEX.matchEntire(tag.trim()) ?: return null
        return m.groupValues.drop(1).map { it.toInt() }
    }

    private val TAG_REGEX = Regex("""v(\d+)\.(\d+)\.(\d+)-custom\.(\d+)""")

    /** True when [release] is newer than the tag this APK was built from. */
    fun isNewer(release: CustomRelease): Boolean {
        val theirs = parseTag(release.tag)
        val ours = parseTag(BUILT_TAG)
        if (theirs == null || ours == null) return release.tag != BUILT_TAG
        for (k in 0 until maxOf(theirs.size, ours.size)) {
            val a = theirs.getOrElse(k) { 0 }
            val b = ours.getOrElse(k) { 0 }
            if (a != b) return a > b
        }
        return false
    }

    fun cacheDir(context: Context): File = File(context.cacheDir, CACHE_DIR).apply { mkdirs() }

    fun apkFile(context: Context, release: CustomRelease): File =
        File(cacheDir(context), "${release.tag}.apk")

    /** A previously completed download for this release, if still on disk. */
    fun downloadedFile(context: Context, release: CustomRelease): File? {
        val f = apkFile(context, release)
        return if (f.isFile && (release.assetSize < 0 || f.length() == release.assetSize)) f else null
    }

    /** Removes cached APKs other than the one for [keep]. */
    fun cleanCache(context: Context, keep: CustomRelease?) {
        val keepName = keep?.let { "${it.tag}.apk" }
        cacheDir(context).listFiles()?.forEach { f ->
            if (f.name != keepName) f.delete()
        }
    }

    /**
     * Streams the release asset to the cache. GitHub answers the asset URL with a redirect to
     * object storage; OkHttp follows it and drops the Authorization header across hosts, which
     * is what that storage requires.
     */
    suspend fun download(
        context: Context,
        release: CustomRelease,
        onProgress: (downloaded: Long, total: Long) -> Unit,
    ): File = withContext(Dispatchers.IO) {
        val target = apkFile(context, release)
        val partial = File(target.path + ".part")
        val request = Request.Builder()
            .url(release.assetApiUrl)
            .github("application/octet-stream")
            .build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw UpdateException("Download failed: HTTP ${response.code}.")
            val body = response.body
            val total = if (release.assetSize > 0) release.assetSize else body.contentLength()
            var done = 0L
            partial.outputStream().use { out ->
                body.byteStream().use { input ->
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        ensureActive()
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        onProgress(done, total)
                    }
                }
            }
        }
        if (release.assetSize > 0 && partial.length() != release.assetSize) {
            partial.delete()
            throw UpdateException("Download incomplete (${partial.length()} of ${release.assetSize} bytes).")
        }
        if (target.exists()) target.delete()
        if (!partial.renameTo(target)) throw UpdateException("Could not move the downloaded file into place.")
        target
    }

    /** Opens the system package installer for [apk]; the user confirms the install there. */
    fun install(context: Context, apk: File) {
        val uri = FileProvider.getUriForFile(context, "${BuildConfig.APPLICATION_ID}.provider.file", apk)
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
    }

    fun formatSize(bytes: Long): String = when {
        bytes < 0 -> "?"
        bytes >= 1024L * 1024L -> String.format(java.util.Locale.US, "%.1f MB", bytes / (1024.0 * 1024.0))
        bytes >= 1024L -> "${bytes / 1024} KB"
        else -> "$bytes B"
    }
}
