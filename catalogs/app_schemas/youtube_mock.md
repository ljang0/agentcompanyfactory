# youtube_mock Schema

**Deploy order**: 93 (alphabetical among all `*_mock` directories; `BASE_PORT=8000` → port 8093)  
**Base URL**: `http://172.17.46.46:8093/`  
**Go Endpoint**: `GET /go?sid=<sid>` → `{initial_state, current_state, state_diff}`  
**Inject**: `POST /post?sid=<sid>` with body `{"action":"set","state":{...}}`

## State Schema

| Key | Type | Description and default |
|-----|------|-------------------------|
| `user` | `User` | Default signed-in user (`user-1`) |
| `videos` | `Video[]` | Long-form videos, including the user's uploads |
| `shorts` | `Short[]` | 20 short-form videos |
| `channels` | `Channel[]` | 16 channels, including `user-1` |
| `comments` | `Record<string, Comment[]>` | Video ID → comments |
| `playlists` | `Playlist[]` | Five default playlists |
| `notifications` | `Notification[]` | Eight defaults |
| `communityPosts` | `Record<string, CommunityPost[]>` | Channel ID → posts |
| `categories` | `string[]` | 16 category labels |
| `settings` | `Settings` | Playback, locale, notification, privacy, and theme settings |

### Entity fields

- User: `{ userId, displayName, email, handle, avatar, subscribedChannels[], watchHistory[], likedVideos[], dislikedVideos[], watchLater[], playlists[], searchHistory[] }`; history item `{videoId,watchedAt}`
- Video: `{ videoId, title, description, channelId, channelName, channelAvatar, thumbnail, duration, uploadDate, viewCount, likeCount, dislikeCount, category, tags[], videoUrl, visibility?, sourceFile? }`
- Short: `{ shortId, title, channelId, channelName, channelAvatar, thumbnail, viewCount, likeCount, dislikeCount, commentCount, uploadDate, isShort }`
- Channel: `{ channelId, name, handle, avatar, banner, description, subscriberCount, videoCount, joinedDate, links[], videos[], verified }`
- Comment: `{ commentId, videoId, userId, userName, userAvatar, text, timestamp, likeCount, dislikeCount, replies[], isPinned, likedBy[] }`; replies use the same shape
- Playlist: `{ playlistId, name, description, creatorId, videoIds[], privacy, createdDate, thumbnail }`
- Notification: `{ notificationId, type, channelId, channelName, channelAvatar, videoId, videoTitle, videoThumbnail, timestamp, isRead, commenterName?, commentSnippet?, milestone? }`
- CommunityPost: `{ postId, channelId, text, timestamp, likeCount, commentCount }`
- Settings: `{ autoplay, captions, subtitlesLang, theme, location, language, notifSubscriptions, notifRecommended, notifActivity, notifReplies, keepWatchHistory, keepSearchHistory }`

## Minimal Inject Example

```json
{
  "type": "chrome_open_url",
  "parameters": {
    "url": "http://172.17.46.46:8093/",
    "inject_state": true,
    "state_content": {
      "action": "set",
      "state": {
        "user": {
          "userId":"user-1", "displayName":"Alex Thompson", "email":"alex.thompson@email.com", "handle":"@alexthompson", "avatar":"https://picsum.photos/100/100?random=current",
          "subscribedChannels":[], "watchHistory":[], "likedVideos":[], "dislikedVideos":[], "watchLater":[], "playlists":[], "searchHistory":[]
        },
        "playlists": []
      }
    }
  }
}
```

The app deep-merges injected objects with defaults, so unmentioned top-level collections remain populated.

## Observable State Changes (for LLM evaluation)

| User Action | State Field Changed |
|-------------|---------------------|
| Watch a video (when watch history is enabled) | `user.watchHistory` (deduplicated `{videoId,watchedAt}` prepended) |
| Like/unlike a video | `user.likedVideos`, `videos[*].likeCount` |
| Dislike/undo dislike on a video | `user.dislikedVideos`, `videos[*].dislikeCount` |
| Add/remove a video from Watch Later | `user.watchLater` |
| Remove one item from or clear watch history | `user.watchHistory` |
| Subscribe/unsubscribe | `user.subscribedChannels`, `channels[*].subscriberCount` |
| Post a top-level comment or reply | `comments[videoId]` |
| Like/unlike a comment or reply | matching comment/reply `likeCount` and `likedBy` |
| Create a playlist | `playlists` (new item), `user.playlists` |
| Add/remove a video in a playlist | `playlists[*].videoIds` |
| Edit playlist name/description | `playlists[*].name`, `playlists[*].description` |
| Move a playlist video up/down | order of `playlists[*].videoIds` |
| Upload a video | `videos` (new item prepended), `comments[newVideoId]`, current-user `channels[*].videos` and `videoCount` |
| Run a search (when search history is enabled) | `user.searchHistory` |
| Clear search history | `user.searchHistory` |
| Mark one/all notifications read | `notifications[*].isRead` |
| Change a setting or theme | `settings.<key>` |

