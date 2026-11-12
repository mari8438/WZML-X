import os
import re

def patch_file(filepath: str):
    with open(filepath, "r", encoding="utf-8") as f:
        code = f.read()

    # 1. Update format_episode_filename and format_season_merge_filename
    old_methods_pattern = re.compile(
        r'    @classmethod\s+def format_episode_filename\(.*?def format_season_merge_filename\(.*?\n        return cls\.sanitize_bluray_keyword\(raw_fn\)',
        re.DOTALL
    )

    new_methods = """    @classmethod
    def format_episode_filename(
        cls,
        anime_name: str,
        season_num: int,
        episode_num: int,
        year: int,
        resolved_meta: Dict[str, str],
    ) -> str:
        \"\"\"
        Produce canonical Starfall episode filename (as edited by user):
        `🄰🅂- [S01E01] A Couple of Cuckoos (2022) 1080p 10bit BluRay HEVC [Tamil-AAC 2.0] ESub ~ neoHEVC ~ [@Anime_Starfall🥰].mkv`
        \"\"\"
        clean_name = re.sub(r"[^\\w\\s-]", "", anime_name).strip()
        quality = cls.sanitize_bluray_keyword(resolved_meta.get("quality", "BluRay"))
        codec_raw = resolved_meta.get("codec", "HEVC")
        codec = "HEVC" if codec_raw.lower() in ("x265", "hevc", "h.265", "h265") else codec_raw
        bit = resolved_meta.get("bit", "10bit")
        resolution = resolved_meta.get("resolution", "1080p")
        group = resolved_meta.get("release_group", "Starfall")
        year_str = f"({year})" if year else ""

        raw_fn = (
            f"🄰🅂- [S{season_num:02d}E{episode_num:02d}] {clean_name} {year_str} "
            f"{resolution} {bit} {quality} {codec} [Tam + Jap AAC 2.0] ESub ~ {group} ~ [@Anime_Starfall🥰].mkv"
        )
        raw_fn = re.sub(r"\\s+", " ", raw_fn).strip()
        return cls.sanitize_bluray_keyword(raw_fn)

    @classmethod
    def format_season_merge_filename(
        cls,
        anime_name: str,
        season_num: int,
        ep_start: int,
        ep_end: int,
        resolved_meta: Dict[str, str],
        year: Optional[int] = None,
    ) -> str:
        \"\"\"
        Produce canonical Starfall Full-Season Merge filename (as edited by user):
        `📦 🄰🅂- [S1E01-02] A Couple of Cuckoos (2022) 1080p 10bit BluRay HEVC [Tamil-AAC 2.0] ESub ~ neoHEVC ~ [@Anime_Starfall🥰].mkv`
        \"\"\"
        clean_name = re.sub(r"[^\\w\\s-]", "", anime_name).strip()
        quality = cls.sanitize_bluray_keyword(resolved_meta.get("quality", "BluRay"))
        codec_raw = resolved_meta.get("codec", "HEVC")
        codec = "HEVC" if codec_raw.lower() in ("x265", "hevc", "h.265", "h265") else codec_raw
        bit = resolved_meta.get("bit", "10bit")
        resolution = resolved_meta.get("resolution", "1080p")
        group = resolved_meta.get("release_group", "Starfall")
        year_str = f"({year})" if year else ""

        raw_fn = (
            f"📦 🄰🅂- [S{season_num}E{ep_start:02d}-{ep_end:02d}] {clean_name} {year_str} "
            f"{resolution} {bit} {quality} {codec} [Tam + Jap AAC 2.0] ESub ~ {group} ~ [@Anime_Starfall🥰].mkv"
        )
        raw_fn = re.sub(r"\\s+", " ", raw_fn).strip()
        return cls.sanitize_bluray_keyword(raw_fn)"""

    if old_methods_pattern.search(code):
        code = old_methods_pattern.sub(lambda m: new_methods, code)
        print("  -> Updated AutoRenameResolver filename methods.")
    else:
        print("  -> Warning: old_methods_pattern did not match, using fallback replace.")

    # 2. Update upload sequencing in upload_season_pack
    old_upload_pattern = re.compile(
        r'        # STAGE 5: STRICT 3-STEP TARGET CHANNEL UPLOAD SEQUENCING.*?(?=        # Final Batch Purge & Quota Release)',
        re.DOTALL
    )

    new_upload = """        # STAGE 5: STRICT 3-STEP TARGET CHANNEL UPLOAD SEQUENCING
        # ====================================================================
        uploaded_msg_ids: List[int] = []
        async with self.upload_season_sem:
            await self.router.start()

            # SINGLE AUTHORITATIVE 16:9 THUMBNAIL FOR ENTIRE SEASON (Merge + All Episodes)
            clean_slug = re.sub(r"[^\\w-]", "_", item.english_title)
            season_thumb: Optional[str] = None
            if item.harvested_thumb_path and os.path.exists(item.harvested_thumb_path):
                season_thumb = item.harvested_thumb_path
            
            if not season_thumb:
                cand1 = os.path.join(self.thumbnails_dir, f"{clean_slug}_S{item.season_number:02d}_thumb.jpg")
                cand2 = os.path.join(self.thumbnails_dir, f"thumb_{clean_slug}_S{item.season_number:02d}E01.jpg")
                if os.path.exists(cand1) and os.path.getsize(cand1) > 1000:
                    season_thumb = cand1
                elif os.path.exists(cand2) and os.path.getsize(cand2) > 1000:
                    season_thumb = cand2

            if not season_thumb and item.poster_card_path and os.path.exists(item.poster_card_path):
                season_thumb = item.poster_card_path

            if not season_thumb and item.muxed_episodes:
                first_ep_num = min(item.muxed_episodes.keys())
                first_ep_path = item.muxed_episodes[first_ep_num]["path"]
                if first_ep_path and os.path.exists(first_ep_path):
                    gen_target = os.path.join(self.thumbnails_dir, f"thumb_{clean_slug}_S{item.season_number:02d}_Season.jpg")
                    self.muxer.generate_16_9_thumbnail(first_ep_path, gen_target, 1440.0)
                    if os.path.exists(gen_target):
                        season_thumb = gen_target

            LOGGER.info(f"🖼️ Authoritative Season 16:9 Thumbnail: {season_thumb}")

            # STEP 1: Post Season Poster Card (Template 11 Hero Card)
            caption_template = USER_SETTINGS.get("POST_ANIME_CAPTION") or (
                "<b>「 {title} - {year} 」</b>\\n━━━━━━━━━━━━━━━━━━\\n"
                "╔════◇═══════════◇════\\n"
                "║ Season ➤ {season}\\n"
                "║ AniList ➤ {rating} Rating\\n"
                "║ Genres ➤ {genres}\\n"
                "║ Quality ➤ {resolution} {bit} BluRay {codec}\\n"
                "║ Audio ➤ Tamil {audio_codec} {audio_channels} ~ {shortsub}\\n"
                "╚════◇═══════════◇════\\n"
                " <blockquote expandable>Synopsis :\\n   {plot}</blockquote>\\n\\n"
                "Dropped By ➤ [@Anime_Starfall🥰]"
            )
            poster_caption = AutoRenameResolver.sanitize_bluray_keyword(
                caption_template.format(
                    title=item.english_title,
                    year=item.year,
                    season=item.season_number,
                    rating=item.rating_str,
                    genres=item.genres_str,
                    resolution=first_meta["resolution"],
                    bit=first_meta["bit"],
                    codec=first_meta.get("codec", "x265"),
                    audio_codec="AAC",
                    audio_channels="2.0",
                    shortsub="ESub",
                    plot=item.clean_synopsis,
                )
            )
            poster_msg_id = await self.router.publish_season_poster(
                source_poster_msg_id=item.source_poster_msg_id,
                photo_path=item.poster_card_path or item.poster_path,
                caption=poster_caption,
            )
            if poster_msg_id:
                uploaded_msg_ids.append(poster_msg_id)

            # STEP 2: Post Full-Season Merged Video File(s)
            if item.merged_season_parts:
                formatted_merge_title = AutoRenameResolver.format_season_merge_filename(
                    item.english_title, item.season_number,
                    min(item.muxed_episodes.keys()), max(item.muxed_episodes.keys()),
                    first_meta,
                    year=item.year,
                )
                merge_caption = f"<b>{formatted_merge_title}</b>"
                for m_part in item.merged_season_parts:
                    m_msg_id = await self.router.upload_file(
                        filepath=m_part["path"],
                        caption=merge_caption,
                        thumb_path=season_thumb,
                        duration_sec=m_part["duration_sec"],
                        width=m_part["width"],
                        height=m_part["height"],
                        cleanup_after_upload=True,
                    )
                    if m_msg_id:
                        uploaded_msg_ids.append(m_msg_id)

            # STEP 3: Post Single Episodes Sequentially
            episodes_payload: List[Dict[str, Any]] = []
            for ep_num in sorted(item.muxed_episodes.keys()):
                ep_data = item.muxed_episodes[ep_num]
                ep_meta = ep_data["resolved_meta"]
                formatted_ep_title = AutoRenameResolver.format_episode_filename(
                    item.english_title, item.season_number, ep_num, item.year, ep_meta
                )
                ep_caption = f"<b>{formatted_ep_title}</b>"
                episodes_payload.append({
                    **ep_data,
                    "thumb_path": season_thumb,
                    "caption": ep_caption,
                    "cleanup_after_upload": True,
                })

            async def _on_episode_purged(reclaimed_bytes: int) -> None:
                self.quota_guard.ram_episode_sem.release()
                await self.quota_guard.release_episode_bytes(item.info_hash, reclaimed_bytes)

            ep_upload_results = await self.router.upload_season_episodes_ordered(
                episodes_payload,
                on_purged_bytes_cb=_on_episode_purged,
            )

            for ep_num in sorted(item.muxed_episodes.keys()):
                ep_data = item.muxed_episodes[ep_num]
                msg_id, bot_tag = ep_upload_results.get(ep_num, (None, "NONE"))
                if msg_id:
                    uploaded_msg_ids.append(msg_id)
                await record_episode_job(
                    season_id=item.season_id,
                    episode_number=ep_num,
                    audio_id=ep_data["audio_id"],
                    bd_relative_path=ep_data["raw_video"],
                    sync_mode=ep_data["sync_mode"],
                    calculated_delay_ms=ep_data["delay_ms"],
                    speed_ratio=ep_data["speed_ratio"],
                    min_psr_score=ep_data["psr"],
                    anchor_telemetry=ep_data["telemetry"],
                    job_state="COMPLETED" if msg_id else "QUARANTINE",
                    tg_single_msg_id=msg_id,
                    uploaded_by_bot=bot_tag,
                )
"""

    if old_upload_pattern.search(code):
        code = old_upload_pattern.sub(lambda m: new_upload, code)
        print("  -> Updated upload_season_pack sequencing, captions, and season thumbnail.")
    else:
        print("  -> Warning: old_upload_pattern did not match.")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"✅ Successfully patched {filepath}")

if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "bot/modules/starfall_pipeline/pipeline_manager.py"
    patch_file(target)
