"""旧话题正文规范化路径的同对象兼容 façade。"""

from autoslice.analysis.topic import normalization as _owner

DANMAKU_META_KEYWORDS = _owner.DANMAKU_META_KEYWORDS
FACADE_EXPORTS = _owner.FACADE_EXPORTS
FRAGMENT_BODY_LINES = _owner.FRAGMENT_BODY_LINES
META_BODY_KEYWORDS = _owner.META_BODY_KEYWORDS
UNSUPPORTED_AI_AUDIENCE_REACTION_RE = _owner.UNSUPPORTED_AI_AUDIENCE_REACTION_RE
clean_body_content = _owner.clean_body_content
filter_unsupported_ai_points = _owner.filter_unsupported_ai_points
is_meta_body_line = _owner.is_meta_body_line
json_points_to_body = _owner.json_points_to_body
normalise_body_line = _owner.normalise_body_line
