package com.example.roomfixture

import androidx.room.ColumnInfo
import androidx.room.Database
import androidx.room.DatabaseView
import androidx.room.Embedded
import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Fts4
import androidx.room.Index
import androidx.room.PrimaryKey
import androidx.room.RoomDatabase

// Version 2 adds, to version 1: a column with a default value, an embedded
// object (its fieldPath is dotted), an entity with a composite primary key and
// a composite index, an FTS4 entity, and a view.
data class Address(
    val city: String?,
    @ColumnInfo(name = "postal_code") val postalCode: String?,
)

@Entity(tableName = "users", indices = [Index(value = ["email"], unique = true)])
data class User(
    @PrimaryKey(autoGenerate = true) val id: Long,
    @ColumnInfo(name = "full_name") val name: String,
    val email: String?,
    @ColumnInfo(name = "is_active", defaultValue = "1") val isActive: Boolean,
    @Embedded(prefix = "addr_") val address: Address?,
)

@Entity(
    tableName = "notes",
    foreignKeys = [
        ForeignKey(
            entity = User::class,
            parentColumns = ["id"],
            childColumns = ["user_id"],
            onDelete = ForeignKey.CASCADE,
        ),
    ],
    indices = [Index("user_id")],
)
data class Note(
    @PrimaryKey val noteId: Long,
    @ColumnInfo(name = "user_id") val userId: Long,
    val body: String,
)

@Entity(
    tableName = "note_tags",
    primaryKeys = ["note_id", "tag"],
    foreignKeys = [
        ForeignKey(
            entity = Note::class,
            parentColumns = ["noteId"],
            childColumns = ["note_id"],
            onDelete = ForeignKey.CASCADE,
            onUpdate = ForeignKey.RESTRICT,
        ),
    ],
    indices = [Index(value = ["tag", "note_id"], name = "idx_note_tags_by_tag")],
)
data class NoteTag(
    @ColumnInfo(name = "note_id") val noteId: Long,
    val tag: String,
)

@Fts4
@Entity(tableName = "note_search")
data class NoteSearch(
    @PrimaryKey @ColumnInfo(name = "rowid") val rowId: Int,
    val body: String,
)

@DatabaseView(
    viewName = "note_summaries",
    value = "SELECT notes.noteId AS noteId, users.full_name AS author, notes.body AS body " +
        "FROM notes INNER JOIN users ON users.id = notes.user_id",
)
data class NoteSummary(
    val noteId: Long,
    val author: String,
    val body: String,
)

@Database(
    entities = [User::class, Note::class, NoteTag::class, NoteSearch::class],
    views = [NoteSummary::class],
    version = 2,
)
abstract class AppDatabase : RoomDatabase()
